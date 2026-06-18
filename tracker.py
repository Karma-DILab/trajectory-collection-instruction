"""Playwright launcher + page wiring."""

import asyncio
import base64
import ctypes
import io
import os
import threading

from playwright.async_api import async_playwright

from recorder import WebActionRecorder
from actions import ActionConverter

try:
    from page_panel import InPagePanel
except Exception:
    InPagePanel = None


# Matches Fara-7B's screen resolution exactly (its system prompt states
# "1428x896"). Both are multiples of 28 — the patch size Qwen2.5-VL (Fara's
# base) tiles images at — so click coordinates land in the same absolute pixel
# space Fara was trained on.
VIEWPORT_W = 1428
VIEWPORT_H = 896
USER_DATA_SUBDIR = "browser_profile"

# Leave room for the Windows taskbar and the --app mode title bar.
SCREEN_RESERVE_W = 20
SCREEN_RESERVE_H = 100

# Height (px) of the floating nav toolbar strip parked ABOVE the browser. The
# browser window is pushed down by this much and the viewport is shrunk to fit,
# so the toolbar never overlaps the page. See nav_toolbar.py.
NAV_BAR_H = 46

# NOTE: the thought panel AND the page lock both live in inject.js now (the card
# is an in-page overlay; window.__webtrack_setlock toggles the dim+block). One
# OS window -> no cross-window focus problem. Python drives it via the exposed
# __webtrack_* callbacks wired up in page_panel.InPagePanel.


def _screen_size():
    """Primary-monitor pixel size (Windows). Falls back to 1920x1080."""
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        u32 = ctypes.windll.user32
        return u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


def _detect_scale_factor(reserve_w=0, reserve_h=0):
    """Return a device-scale factor in (0, 1.0] that lets a 1428x896 CSS
    viewport fit within the user's primary monitor, optionally leaving
    `reserve_w` px free on the right and `reserve_h` px free on top (for the
    floating nav toolbar). Windows-only. Caps at 1.0 — never up-scales on large
    displays.

    NOTE: only the *display* scale shrinks; the CSS viewport stays 1428x896,
    so click coordinates remain in 0-1428 / 0-896 regardless of the panel."""
    sw, sh = _screen_size()
    usable_w = max(sw - SCREEN_RESERVE_W - reserve_w, 800)
    usable_h = max(sh - SCREEN_RESERVE_H - reserve_h, 600)
    return min(usable_w / VIEWPORT_W, usable_h / VIEWPORT_H, 1.0)


def _make_window_unresizable(expect_left, expect_top, expect_w, expect_h, tol=24):
    """Strip the resize border (WS_THICKFRAME) and the maximize box from the
    tracked Chromium --app window so the worker physically cannot drag-resize it
    (a resize would distort the fixed 1428x896 screenshots). Windows-only,
    best-effort.

    The window is matched by class + geometry against the bounds Chromium
    reports, so the worker's OTHER normal Chrome windows are never touched.
    Returns True if a window was found and locked."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return False

    GWL_STYLE = -16
    WS_THICKFRAME = 0x00040000
    WS_MAXIMIZEBOX = 0x00010000
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020

    best = [None, 10 ** 9]   # [hwnd, score]

    def _class_name(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if _class_name(hwnd) != "Chrome_WidgetWin_1":
                return True
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            score = (abs(r.left - expect_left) + abs(r.top - expect_top)
                     + abs(w - expect_w) + abs(h - expect_h))
            if score < best[1]:
                best[1], best[0] = score, hwnd
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumProc(_cb), 0)
    except Exception:
        return False

    hwnd = best[0]
    # Require a plausible geometry match so we never strip styles off an
    # unrelated Chrome window the worker happens to have open.
    if hwnd is None or best[1] > 4 * tol:
        return False
    try:
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE,
                              style & ~WS_THICKFRAME & ~WS_MAXIMIZEBOX)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        return True
    except Exception:
        return False


async def cdp_screenshot(page, *, fmt="jpeg", quality=95):
    """Capture via CDP with fromSurface=False so Windows DWM does the window
    grab instead of forcing a GPU-surface readback in the renderer. The
    renderer's frame pipeline is NOT paused, so the visible page does not
    blink during background captures.

    The capture is at physical-pixel size (CSS viewport * device scale). We
    upsample to VIEWPORT_W x VIEWPORT_H so every saved screenshot has the
    same dimensions regardless of which user recorded it — click coordinates
    are stored in CSS pixels (0-1428 / 0-896) so no coord rescaling is needed.

    The nav toolbar lives in a SEPARATE OS window (see nav_toolbar.py), which
    this page-surface grab never sees, so there is nothing of ours to hide here.
    """
    client = await page.context.new_cdp_session(page)
    try:
        params = {"format": fmt, "fromSurface": False, "captureBeyondViewport": False}
        if fmt == "jpeg":
            params["quality"] = quality
        result = await client.send("Page.captureScreenshot", params)
        raw = base64.b64decode(result["data"])
    finally:
        try:
            await client.detach()
        except Exception:
            pass

    def _resize(raw_bytes):
        from PIL import Image
        img = Image.open(io.BytesIO(raw_bytes))
        if img.size == (VIEWPORT_W, VIEWPORT_H):
            return raw_bytes
        img = img.resize((VIEWPORT_W, VIEWPORT_H), Image.LANCZOS)
        out = io.BytesIO()
        if fmt == "jpeg":
            img.convert("RGB").save(out, format="JPEG", quality=quality)
        else:
            img.save(out, format="PNG")
        return out.getvalue()

    try:
        # Run the PIL decode/resize/encode in a worker thread: it's CPU-bound
        # and would otherwise block the event loop on every capture (the bg
        # cache fires every 0.5s), delaying the very action-handling that feeds
        # the thought panel. Off-loop keeps the panel snappy.
        return await asyncio.to_thread(_resize, raw)
    except Exception as e:
        print(f"  [warn] screenshot resize failed, using raw: {e}")
        return raw


async def run_tracker(task_description, session_dir, start_url,
                       stop_flag=None, progress=None, terminate_status=None,
                       collect_thoughts=True):
    """Launch persistent Chromium, instrument the page, record until stop.

    Args:
        task_description: task label
        session_dir:      output dir for the session
        start_url:        starting URL (default google.com)
        stop_flag:        threading.Event from the HTTP server. If provided,
                          stdin is NOT read; instead the loop ends when this
                          flag is set (server mode). If None, stdin Enter is
                          the stop signal (console mode).
        progress:         dict to receive live progress {step_count, status}.
                          Optional; passed by the server for status polling.

    Returns the WebActionRecorder so the caller can decide to keep/discard.
    """
    state_dir = os.environ.get("WEBTRACKER_STATE_DIR") or os.path.dirname(os.path.abspath(__file__))
    assets_dir = os.environ.get("WEBTRACKER_ASSETS_DIR") or state_dir
    user_data_dir = os.path.join(state_dir, USER_DATA_SUBDIR)
    os.makedirs(user_data_dir, exist_ok=True)
    inject_js_path = os.path.join(assets_dir, "inject.js")

    # Use Chromium --app mode to hide browser chrome (no URL bar, no tab bar).
    # All user actions are forced into the page viewport, matching Fara's setup.
    app_url = start_url if start_url and start_url != "about:blank" else "https://www.google.com/?hl=en"

    # Auto-shrink the rendering on small/HiDPI screens so the full 1428x896
    # CSS viewport always fits in the window — no clipping, identical layout
    # across all users. The thought panel now lives INSIDE the page (it overlays
    # the right edge only while a thought is pending), so we no longer reserve a
    # strip of the screen for a separate window.
    scale = _detect_scale_factor(reserve_w=0, reserve_h=NAV_BAR_H)
    win_w = int(VIEWPORT_W * scale)
    win_h = int(VIEWPORT_H * scale)
    print(f"  [layout] device-scale={scale:.3f}, window={win_w}x{win_h}, "
          f"CSS viewport=1428x896, nav-bar={NAV_BAR_H}px")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            # Force English: locale sets the Accept-Language header AND
            # navigator.language(s), so sites render in English instead of the
            # OS locale (ko-KR). --lang sets Chromium's own UI language.
            locale="en-US",
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            device_scale_factor=scale,
            args=[
                "--no-default-browser-check",
                "--no-first-run",
                "--lang=en-US",
                "--disable-blink-features=AutomationControlled",
                f"--app={app_url}",
                f"--window-size={win_w},{win_h}",
                f"--window-position=0,{NAV_BAR_H}",
                f"--force-device-scale-factor={scale}",
                # macOS flicker-mitigation flag:
                #  PaintHolding causes a brief white repaint during the screenshot pause.
                #  (Note: --disable-gpu-vsync was removed because on Windows it
                #  conflicts with the DWM compositor and causes visible flicker.)
                "--disable-features=PaintHolding,IsolateOrigins,site-per-process",
                # Bot-detection mitigation (Tesla, Cloudflare, Akamai, etc.):
                "--disable-infobars",
                "--disable-extensions",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
            ],
            ignore_default_args=["--enable-automation"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        # Stealth init script: hides common automation flags BEFORE any page
        # script runs (helps with Tesla, Cloudflare, Akamai bot detection).
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
            window.chrome = window.chrome || { runtime: {} };
            const origQuery = window.navigator.permissions && window.navigator.permissions.query;
            if (origQuery) {
              window.navigator.permissions.query = (p) => (
                p && p.name === 'notifications'
                  ? Promise.resolve({ state: Notification.permission })
                  : origQuery(p)
              );
            }
        """)

        # NOTE: inject.js is registered LATER, AFTER expose_function — init
        # scripts run in registration order on every page, and inject.js calls
        # the exposed bindings (__webtrack_pending_card) at startup, so the
        # bindings must be installed first.

        # In --app mode, Chromium opens the app URL automatically.
        # Wait briefly for the first page to appear.
        if not context.pages:
            try:
                page = await context.wait_for_event("page", timeout=5000)
            except Exception:
                page = await context.new_page()
                await page.goto(app_url)
        else:
            page = context.pages[0]

        recorder = WebActionRecorder(
            page, session_dir, task_description,
            viewport_size=(VIEWPORT_W, VIEWPORT_H),
        )
        converter = ActionConverter(recorder, emit_wait=not collect_thoughts)

        # In-page thought panel: the card UI lives INSIDE the page (inject.js),
        # so there is exactly one OS window and no cross-window keyboard-focus
        # fight. Each action locks the page + shows a card; the user types the
        # thought right there and submits. Console mode gets an "작업 종료" button
        # in the card that sets this event to stop the run.
        console_finish = threading.Event() if stop_flag is None else None

        panel = None
        if collect_thoughts and InPagePanel is not None:
            panel = InPagePanel(
                recorder, task=task_description,
                on_finish=(console_finish.set if console_finish is not None else None))
            recorder.card_callback = panel.enqueue

        # Expose Python callbacks for the injected JS to invoke. The event
        # handler is wrapped to flag the panel as "processing" while an input is
        # in flight, so a page that navigates mid-action keeps itself locked
        # until the resulting card actually appears (no unlock-and-leak race).
        async def _on_event(ev):
            if panel is not None:
                panel._processing += 1
            try:
                await converter.handle_event(ev)
            finally:
                if panel is not None:
                    panel._processing -= 1
        await context.expose_function("__webtrack_event", _on_event)

        if panel is not None:
            # thought submitted from the in-page textarea
            await context.expose_function("__webtrack_thought", panel.on_thought)
            # a freshly-loaded page asks whether to re-show an unanswered card
            await context.expose_function("__webtrack_pending_card", panel.pending_card)
            # the in-page "작업 종료" button
            await context.expose_function("__webtrack_finish", panel.finish)

        # Inject event-capture + in-page panel JS into every page (every
        # navigation too). Registered AFTER the bindings above so that on each
        # page the bindings exist by the time inject.js calls them at startup.
        await context.add_init_script(path=inject_js_path)

        # The first page was already loaded by --app before add_init_script /
        # expose_function were registered, so inject.js is NOT active on it yet.
        # Reload it so the init script and the exposed binding take effect,
        # otherwise the first page's input (e.g. typing into Google) is lost.
        try:
            await page.reload()
            await page.wait_for_load_state("domcontentloaded")
        except Exception as e:
            print(f"  [warn] initial reload failed: {e}")

        # Refresh the in-memory screenshot cache when a page finishes loading.
        # If the user X-closes after navigating somewhere new, the cached PNG
        # we fall back to for the terminate screenshot reflects the page they
        # were actually looking at, instead of the page from the last action.
        async def _refresh_cache_for(p):
            try:
                png = await cdp_screenshot(p, fmt="png")
                recorder.last_png_bytes = png
            except Exception:
                pass

        def _hook_cache_refresh(p):
            try:
                p.on("load", lambda: asyncio.ensure_future(_refresh_cache_for(p)))
            except Exception:
                pass

        # Single-tab enforcement: if a new page sneaks past the inject.js
        # defences (some sites use trusted-event window.open variants or
        # native target=_blank), capture its URL, close it, and navigate the
        # MAIN page there so recording stays glued to one tab.
        async def _suppress_new_page(new_page):
            target_url = ""
            try:
                # Wait briefly for the URL to resolve from about:blank.
                await new_page.wait_for_event("framenavigated", timeout=2000)
            except Exception:
                pass
            try:
                target_url = new_page.url or ""
            except Exception:
                target_url = ""
            try:
                await new_page.close()
            except Exception:
                pass
            if target_url and target_url not in ("about:blank", ""):
                try:
                    await recorder.page.goto(target_url)
                except Exception as e:
                    print(f"  [warn] could not redirect to {target_url}: {e}")

        def _on_page(new_page):
            print(f"  [info] suppressing new tab: {new_page.url}")
            asyncio.create_task(_suppress_new_page(new_page))
        context.on("page", _on_page)

        _hook_cache_refresh(page)

        # Start the wait-action idle timer.
        await converter.start_wait_timer()

        # Floating nav toolbar (back / forward / reload) in its OWN OS window,
        # parked in the strip ABOVE the browser. A separate window is the whole
        # point: the CDP page-surface grab never captures it (no screenshot
        # pollution, no per-capture flicker), and clicks don't steal browser
        # focus (WS_EX_NOACTIVATE). A click records the matching keyboard-
        # shortcut action and performs the real navigation — same path the real
        # Alt+Left / Alt+Right / F5 keys already take.
        nav_stop = None
        try:
            from nav_toolbar import start_nav_toolbar
            _loop = asyncio.get_running_loop()

            async def do_nav(direction):
                if panel is not None:
                    panel._processing += 1
                try:
                    await converter.handle_nav(direction)
                finally:
                    if panel is not None:
                        panel._processing -= 1

            _nav_thread, nav_stop = start_nav_toolbar(
                _loop, do_nav, width=win_w, height=NAV_BAR_H)
        except Exception as e:
            print(f"  [warn] nav toolbar not started: {e}")

        # Background screenshot cache — refreshes recorder.last_png_bytes at a
        # low rate so every action handler can use the PRE-action page state
        # (avoids races where the user's keydown triggers navigation before
        # we can screenshot from Python).
        cache_stop = asyncio.Event()
        async def _bg_cache_refresh():
            while not cache_stop.is_set():
                try:
                    pg = recorder.page
                    # Skip while a thought is pending: the page is locked and the
                    # in-page card overlay is up, so a capture now would bake the
                    # overlay into the cached frame (and thus the next action's
                    # screenshot). Keep the last clean pre-lock frame instead.
                    if pg is not None and (panel is None or panel.pending_count() == 0):
                        png = await cdp_screenshot(pg, fmt="jpeg", quality=95)
                        recorder.last_png_bytes = png
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(cache_stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        cache_task = asyncio.create_task(_bg_cache_refresh())

        # Window-size guard: a worker drag-resizing the window would distort the
        # fixed 1428x896 screenshots. Two layers: (1) strip the OS resize border
        # so it can't be dragged at all; (2) a CDP watchdog that snaps the bounds
        # back if anything changes them (covers the rare case where (1) couldn't
        # locate the window). Wholly best-effort — never breaks the run.
        async def _guard_window_size():
            try:
                bclient = await context.new_cdp_session(page)
                wid = (await bclient.send("Browser.getWindowForTarget"))["windowId"]
                target = dict((await bclient.send(
                    "Browser.getWindowBounds", {"windowId": wid}))["bounds"])
            except Exception as e:
                print(f"  [warn] window-size guard disabled: {e}")
                return
            target.pop("windowState", None)   # only pin left/top/width/height
            try:
                locked = await asyncio.to_thread(
                    _make_window_unresizable,
                    target.get("left", 0), target.get("top", NAV_BAR_H),
                    target.get("width", win_w), target.get("height", win_h))
                print(f"  [layout] window resize "
                      f"{'disabled (border removed)' if locked else 'guarded by watchdog'}")
            except Exception:
                pass
            while not cache_stop.is_set():
                try:
                    cur = (await bclient.send(
                        "Browser.getWindowBounds", {"windowId": wid}))["bounds"]
                    if any(cur.get(k) != target.get(k)
                           for k in ("left", "top", "width", "height")):
                        await bclient.send("Browser.setWindowBounds",
                                           {"windowId": wid, "bounds": target})
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(cache_stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        guard_task = asyncio.create_task(_guard_window_size())

        # Background progress reporter for server mode (polled by frontend).
        progress_task = None
        if progress is not None:
            async def _report_progress():
                try:
                    while True:
                        progress["step_count"] = recorder.step
                        await asyncio.sleep(0.4)
                except asyncio.CancelledError:
                    return
            progress_task = asyncio.create_task(_report_progress())

        # Wait until the user closes the browser window.
        closed = asyncio.Event()

        def _on_closed(*args):
            # Cancel pending timers immediately so they don't fire after the
            # page is gone (which would cause TargetClosedError noise).
            converter._wait_active = False
            if converter._wait_task and not converter._wait_task.done():
                converter._wait_task.cancel()
            if converter._scroll_task and not converter._scroll_task.done():
                converter._scroll_task.cancel()
            closed.set()

        context.on("close", _on_closed)
        page.on("close", _on_closed)

        if stop_flag is None:
            print("\n>>> Browser is open. Perform your task there. <<<")
            print(">>> When FINISHED, click '작업 종료' in the side panel (or press ENTER here). <<<")
            print(">>> (Closing the browser window also stops, but may miss the final screenshot.) <<<\n")
        else:
            print("\n>>> Tracking started via web UI. Use the 'Finish' button there to stop. <<<\n")

        # End on whichever happens first:
        #  * console mode: the panel's "작업 종료" button OR stdin Enter
        #  * server mode:  stop_flag set by /stop endpoint
        #  * either mode:  browser window closed
        closed_task = asyncio.ensure_future(closed.wait())

        stop_waiters = {closed_task}
        if stop_flag is None:
            loop = asyncio.get_event_loop()
            stdin_task = loop.run_in_executor(None, input)
            stop_waiters.add(stdin_task)

            async def _wait_console_finish():
                while not console_finish.is_set():
                    await asyncio.sleep(0.15)
            stop_waiters.add(asyncio.create_task(_wait_console_finish()))
        else:
            async def _wait_for_stop_flag():
                while not stop_flag.is_set():
                    await asyncio.sleep(0.2)
            stop_waiters.add(asyncio.create_task(_wait_for_stop_flag()))

        try:
            _, pending = await asyncio.wait(
                stop_waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except KeyboardInterrupt:
            pending = set(stop_waiters)

        browser_alive = not closed.is_set()

        for t in pending:
            t.cancel()

        # Stop progress reporter.
        if progress_task and not progress_task.done():
            progress_task.cancel()

        # Stop the background screenshot cache loop.
        cache_stop.set()
        if cache_task and not cache_task.done():
            cache_task.cancel()

        # Stop the window-size guard (its loop also watches cache_stop).
        if guard_task and not guard_task.done():
            guard_task.cancel()

        # Close the floating nav toolbar window.
        if nav_stop is not None:
            nav_stop.set()

        # Stop idle timer and flush buffered actions.
        await converter.stop_wait_timer()
        await converter.flush_all()

        # Record terminate. If the browser is still alive, capture the FINAL
        # page as the terminate screenshot; otherwise reuse the last one.
        # The outcome chosen on the in-page 성공/실패/다시하기 buttons wins;
        # otherwise fall back to the server-provided status callback.
        try:
            if panel is not None and getattr(panel, "outcome", None):
                _status = panel.outcome
            else:
                _status = terminate_status() if callable(terminate_status) else "success"
        except Exception:
            _status = "success"
        if _status not in ("success", "fail", "retry"):
            _status = "success"
        if browser_alive:
            # Grab a FRESH, CLEAN shot of the final page for the terminate card
            # (hide our own UI so it's the actual page, not the dimmed overlay).
            try:
                pg = recorder.page
                await pg.evaluate(
                    "() => { let s=document.getElementById('__wt_term_hide');"
                    " if(!s){s=document.createElement('style');s.id='__wt_term_hide';"
                    " s.textContent='[data-webtrack]{visibility:hidden!important}';"
                    " (document.head||document.documentElement).appendChild(s);} }")
                await pg.evaluate(
                    "() => new Promise(r => requestAnimationFrame("
                    "() => requestAnimationFrame(r)))")
                recorder.last_png_bytes = await cdp_screenshot(pg, fmt="jpeg", quality=95)
                await pg.evaluate(
                    "() => { let s=document.getElementById('__wt_term_hide');"
                    " if(s) s.remove(); }")
            except Exception:
                pass   # fall back to the cached frame
            try:
                await recorder.record({"action": "terminate", "status": _status})
            except Exception:
                if recorder.step > 0:
                    _write_terminate_entry(recorder, status=_status)
        elif recorder.step > 0:
            _write_terminate_entry(recorder, status=_status)

        # Wait (browser still open) for the user to fill every pending in-page
        # card — including the terminate card just enqueued — before finalizing.
        # Bail out if they X-close the browser, since then there's no page left
        # to show the cards in.
        if panel is not None:
            try:
                if browser_alive and recorder.step > 0 and panel.pending_count() > 0:
                    print(">>> 페이지의 생각 패널에 각 행동의 생각을 입력하세요. <<<")
                    while panel.pending_count() > 0 and not closed.is_set():
                        await asyncio.sleep(0.2)
            except Exception:
                pass
            if browser_alive:
                try:
                    await panel.close()   # lift the lock before closing
                except Exception:
                    pass

        recorder.finalize()

        # Close the browser if the user ended via console.
        if browser_alive:
            try:
                await context.close()
            except Exception:
                pass

    return recorder


def _write_terminate_entry(recorder, status="success"):
    """Append a terminate entry without invoking page.screenshot().

    Prefers the cached bytes from the recorder (captured at the moment of the
    last user action) so that even when the browser is closed via the X button
    we still produce a fresh PNG instead of just reusing the prior step's file.
    """
    import json
    import os

    next_step = recorder.step + 1

    # Try to write a fresh terminate screenshot from the cached PNG bytes.
    shot_rel = f"screenshot/{recorder.step:04d}.jpg" if recorder.step > 0 else ""
    if getattr(recorder, "last_png_bytes", None):
        name = f"{next_step:04d}.jpg"
        try:
            with open(os.path.join(recorder.screenshot_dir, name), "wb") as f:
                f.write(recorder.last_png_bytes)
            shot_rel = f"screenshot/{name}"
        except Exception as e:
            print(f"  [warn] could not write fallback terminate screenshot: {e}")

    recorder.step = next_step
    entry = {
        "timestamp": __import__("utils").get_current_time(),
        "screenshot": shot_rel,
        "viewport_size": recorder.viewport_size,
        "action": {"action": "terminate", "status": status},
        "thought": "",
    }
    # Hold the recorder's io lock: the popup thread may be rewriting the whole
    # file via set_thought() for an earlier card at the same moment, which would
    # otherwise drop this freshly appended terminate line.
    with recorder._io_lock:
        with open(recorder.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{recorder.step:>3}] {entry['action']}")

    # Enqueue a final thought card for terminate too. record() normally fires
    # this, but the X-close path comes through here instead — without this the
    # worker would never be asked for a closing thought when they end by closing
    # the browser window. The browser is gone, so there's nothing to shield; the
    # popup (a separate window) is still up and the caller's wait loop blocks on
    # it until the thought is written.
    if getattr(recorder, "card_callback", None):
        try:
            shot_abs = os.path.join(recorder.screenshot_dir, f"{recorder.step:04d}.jpg")
            recorder.card_callback(recorder.step, entry["action"], shot_abs)
        except Exception as e:
            print(f"  [warn] terminate card_callback failed: {e}")
