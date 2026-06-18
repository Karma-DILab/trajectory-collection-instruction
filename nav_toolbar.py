"""Floating navigation toolbar (back / forward / reload) for the web tracker.

It lives in its OWN top-level OS window (Tkinter), parked in a slim strip ABOVE
the tracked Chromium window. Being a SEPARATE window is the whole point:

  * The screenshot path captures only the BROWSER's own surface (CDP
    Page.captureScreenshot), so this window never lands in a saved screenshot —
    no training-data pollution, and no need to hide/flash it on every capture
    (which is exactly what made an in-page bar flicker).
  * WS_EX_NOACTIVATE keeps button clicks from stealing keyboard focus from the
    browser, so the worker can click Back and keep typing — no focus dance, and
    none of the cross-window focus problems an in-page text box would have had.

A click schedules the async nav handler on the tracker's asyncio loop via
run_coroutine_threadsafe; that handler records the matching keyboard-shortcut
action (Alt+Left / Alt+Right / F5) and performs the real navigation.

Tkinter is stdlib; if it is somehow unavailable the tracker keeps working and
the worker can still use the real keyboard shortcuts (which are recorded too).
"""

import asyncio
import threading


# (glyph, korean label, direction) for each button, left-to-right.
_BUTTONS = (
    ("←", "뒤로",     "back"),     # ←
    ("→", "앞으로",   "forward"),  # →
    ("↻", "새로고침", "reload"),   # ↻
)


def start_nav_toolbar(loop, on_nav, *, width, height=46, x=0, y=0):
    """Spawn the toolbar in its own daemon thread.

    loop:   the asyncio loop running the tracker (for run_coroutine_threadsafe)
    on_nav: coroutine function on_nav(direction) -> records + navigates
    width:  bar width in physical px (the process is DPI-aware, so Tk geometry
            px line up with Chrome's --window-size/position px)
    Returns (thread, stop_event); set stop_event to close the window.
    """
    stop_event = threading.Event()

    def _run():
        try:
            import tkinter as tk
        except Exception as e:
            print(f"  [warn] nav toolbar unavailable (tkinter missing): {e}")
            return

        try:
            root = tk.Tk()
        except Exception as e:
            print(f"  [warn] nav toolbar could not open a window: {e}")
            return

        root.overrideredirect(True)               # no title bar / borders
        root.attributes("-topmost", True)         # float above the browser
        root.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
        root.configure(bg="#202124")

        def fire(direction):
            try:
                asyncio.run_coroutine_threadsafe(on_nav(direction), loop)
            except Exception as e:
                print(f"  [warn] nav dispatch failed: {e}")

        bar = tk.Frame(root, bg="#202124")
        bar.pack(fill="both", expand=True)

        for glyph, label, direction in _BUTTONS:
            b = tk.Button(
                bar, text=f"{glyph}  {label}",
                command=(lambda d=direction: fire(d)),
                font=("Segoe UI Symbol", 11), fg="#e8eaed", bg="#3c4043",
                activebackground="#5f6368", activeforeground="#ffffff",
                bd=0, relief="flat", cursor="hand2", padx=12, pady=4,
            )
            b.pack(side="left", padx=(8, 0), pady=6)

        # Force the window to actually map + raise. Override-redirect windows
        # created from a worker thread don't always show on their own.
        root.update_idletasks()
        try:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.update()
        except Exception:
            pass
        print(f"[navbar] nav toolbar window up: {int(width)}x{int(height)} "
              f"at +{int(x)}+{int(y)} (top-left of screen, above the browser)")

        # Best-effort: mark the window non-activating + tool-window so clicks
        # never pull foreground/keyboard focus off the browser. Windows-only;
        # silently ignored elsewhere. Done AFTER the window is shown so it can
        # never block the initial map.
        def _no_activate():
            try:
                import ctypes
                GWL_EXSTYLE = -20
                WS_EX_NOACTIVATE = 0x08000000
                WS_EX_TOOLWINDOW = 0x00000080
                user32 = ctypes.windll.user32
                hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
                cur = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(
                    hwnd, GWL_EXSTYLE, cur | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            except Exception:
                pass
        _no_activate()

        def _tick():
            if stop_event.is_set():
                try:
                    root.destroy()
                except Exception:
                    pass
                return
            try:
                root.attributes("-topmost", True)   # re-assert if something stole it
            except Exception:
                pass
            root.after(250, _tick)

        root.after(250, _tick)
        try:
            root.mainloop()
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, stop_event
