"""Raw browser events -> Fara-style action dicts.

Filtering rules:
- Only left mouse button (others ignored)
- OS shortcuts (Win+*, Alt+Tab, Alt+F4) ignored
- Modifier-only keypress ignored
- Single printable char w/o Ctrl/Alt -> buffered into `type`
- Other keys / Ctrl-Alt combos -> emitted as `key`
- Wheel events accumulated, flushed after 500ms idle, as `scroll` with pixel delta
  (Fara: positive=up, wheel.deltaY: positive=down, so we invert)
"""

import asyncio

from utils import normalize_key_name


SCROLL_FLUSH_DELAY = 0.5      # seconds idle -> commit one scroll gesture
TYPE_FLUSH_DELAY = 1.5        # seconds idle -> commit one typing burst
BACKSPACE_FLUSH_DELAY = 1.0   # seconds idle -> commit one backspace run
WAIT_INTERVAL = 12.0         # idle seconds -> emit a `wait` action (when enabled)


class ActionConverter:
    def __init__(self, recorder, emit_wait=True):
        self.recorder = recorder
        # When False (live-thought mode) we never emit idle `wait` actions —
        # every recorded action triggers a thought card, and a `wait` card is
        # meaningless. Type/scroll bursts still self-flush via their own timers.
        self.emit_wait = emit_wait
        self.type_buffer = ""
        self.type_coord = None       # cursor coord for the buffered text
        self.scroll_accum = 0
        self._scroll_pre_png = None  # cached frame snapshotted at scroll-gesture start
        self.backspace_count = 0     # consecutive standalone backspaces pending
        self._scroll_task = None
        self._type_task = None
        self._backspace_task = None
        self._wait_task = None
        self._wait_active = False
        self._lock = asyncio.Lock()

    # ---------- entry point ----------

    async def handle_event(self, ev):
        self._reset_wait_timer()  # any user input resets idle countdown
        async with self._lock:
            try:
                t = ev.get("type")
                if t == "click":
                    await self._on_click(ev)
                elif t == "keydown":
                    await self._on_keydown(ev)
                elif t == "wheel":
                    await self._on_wheel(ev)
            except Exception as e:
                print(f"  [error in handle_event] {type(e).__name__}: {e}")

    async def flush_all(self):
        """Flush any buffered state. Call before saving / shutting down."""
        async with self._lock:
            if self._scroll_task and not self._scroll_task.done():
                self._scroll_task.cancel()
            if self._type_task and not self._type_task.done():
                self._type_task.cancel()
            if self._backspace_task and not self._backspace_task.done():
                self._backspace_task.cancel()
            await self._flush_type()
            await self._flush_scroll()
            await self._flush_backspace()

    # ---------- nav toolbar (back / forward / reload) ----------

    # event.key for each on-screen nav button — chosen so the recorded `key`
    # action is byte-identical to what _on_keydown produces when the worker
    # presses the real shortcut instead of clicking the button.
    _NAV_KEYS = {
        "back":    ["Alt", "ArrowLeft"],
        "forward": ["Alt", "ArrowRight"],
        "reload":  ["F5"],
    }

    async def handle_nav(self, direction):
        """A nav button was clicked in the page. Record it as the equivalent
        keyboard shortcut (so the trajectory looks exactly as if the worker had
        pressed Alt+Left / Alt+Right / F5) and then perform the REAL navigation
        through Playwright — a synthetic key event cannot drive the browser's
        own back/forward/reload."""
        keys = self._NAV_KEYS.get(direction)
        if keys is None:
            return
        self._reset_wait_timer()
        async with self._lock:
            await self._flush_type()
            await self._flush_scroll()
            await self._flush_backspace()
            # Default (cached) screenshot is the pre-navigation page — the
            # correct observation for "the worker chose to go back here".
            await self.recorder.record({"action": "key", "keys": keys})

        # Drive the actual navigation AFTER recording, outside the lock. goBack /
        # goForward are no-ops at the ends of history; reload always applies.
        page = self.recorder.page
        try:
            if direction == "back":
                await page.go_back()
            elif direction == "forward":
                await page.go_forward()
            elif direction == "reload":
                await page.reload()
        except Exception as e:
            print(f"  [warn] nav '{direction}' failed: {e}")


    # ---------- idle wait timer ----------

    async def start_wait_timer(self):
        self._wait_active = True
        self._reset_wait_timer()

    async def stop_wait_timer(self):
        self._wait_active = False
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()

    def _reset_wait_timer(self):
        if not self._wait_active or not self.emit_wait:
            return
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()
        self._wait_task = asyncio.create_task(self._wait_after_delay(WAIT_INTERVAL))

    async def _wait_after_delay(self, delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._flush_type()
            await self._flush_scroll()
            await self._flush_backspace()
            await self.recorder.record({"action": "wait", "time": int(delay)})
        # re-arm so consecutive idle periods emit more `wait`s
        self._reset_wait_timer()

    # ---------- handlers ----------

    async def _on_click(self, ev):
        if ev.get("button") != 0:
            return  # right/middle ignored
        await self._flush_type()
        await self._flush_scroll()
        await self._flush_backspace()
        coord = [int(ev["x"]), int(ev["y"])]
        self.type_coord = coord
        await self.recorder.record({"action": "left_click", "coordinate": coord})

    async def _on_keydown(self, ev):
        key = ev.get("key", "")
        if key in ("Control", "Alt", "Shift", "Meta"):
            return  # modifier-only press

        ctrl = bool(ev.get("ctrlKey"))
        alt = bool(ev.get("altKey"))
        shift = bool(ev.get("shiftKey"))
        meta = bool(ev.get("metaKey"))

        non_shift_mods = []
        if ctrl: non_shift_mods.append("Control")
        if alt: non_shift_mods.append("Alt")
        if meta: non_shift_mods.append("Meta")

        # OS-level shortcut filtering
        if meta:
            return
        if alt and key in ("Tab", "F4"):
            return

        # Plain printable char (no Ctrl/Alt) -> buffer for typing
        if not non_shift_mods and len(key) == 1 and (key.isprintable() or key == " "):
            await self._flush_backspace()   # a real char ends any delete run
            self.type_buffer += key
            self._restart_type_timer()
            return

        # Backspace while typing -> backspace inside buffer (fixes the current
        # burst; stays part of the same `type` action).
        if not non_shift_mods and key == "Backspace" and self.type_buffer:
            self.type_buffer = self.type_buffer[:-1]
            self._restart_type_timer()
            return

        # Backspace with no active typing buffer -> the worker is deleting
        # committed/existing text. Accumulate consecutive presses into ONE `key`
        # action so a whole delete run is a single action + a single thought
        # card, not one per keystroke.
        if not non_shift_mods and key == "Backspace":
            await self._flush_scroll()
            self.backspace_count += 1
            self._restart_backspace_timer()
            return

        # Plain Enter while a typing burst is still buffered (not yet flushed by
        # the debounce/click) -> the user typed-then-submitted in one go. Record
        # it as a single `search` action instead of two (`type` + `key Enter`),
        # which is how people actually search: "와다다다" + Enter. When the type
        # buffer is already empty (they paused, debounce flushed it) Enter falls
        # through below as a standalone `key` action. Shift/Ctrl/Alt+Enter also
        # fall through (those aren't "submit").
        if key == "Enter" and not non_shift_mods and not shift and self.type_buffer:
            if self._type_task and not self._type_task.done():
                self._type_task.cancel()
            await self._flush_scroll()
            text = self.type_buffer
            self.type_buffer = ""
            entry = {"action": "search", "text": text}
            if self.type_coord is not None:
                entry["coordinate"] = self.type_coord
            # Use the cached frame (captured ~every 0.5s while typing, so it
            # already shows the query): instant card, and no race with the
            # Enter's navigation that a live capture would lose.
            await self.recorder.record(entry, fresh=False)
            return

        # Anything else -> emit `key` action
        await self._flush_type()
        await self._flush_scroll()
        await self._flush_backspace()
        mods = list(non_shift_mods)
        if shift:
            mods.append("Shift")

        nk = normalize_key_name(key)
        if not nk:
            return
        if len(nk) == 1 and nk.isalpha():
            nk = nk.upper()  # Fara convention for hotkey letters

        await self.recorder.record({"action": "key", "keys": mods + [nk]})

    async def _on_wheel(self, ev):
        # A scroll counts as switching to a new "intent" — flush any pending
        # type buffer / backspace run so the trajectory stays in
        # {type, scroll, ...} order instead of cutting into them silently.
        await self._flush_type()
        await self._flush_backspace()
        # Snapshot the PRE-scroll frame at the START of a new scroll gesture
        # (scroll_accum == 0 means nothing is accumulated yet, so this wheel
        # begins a fresh gesture). Scroll is debounced by SCROLL_FLUSH_DELAY, and
        # the background screenshot cache keeps advancing during that window — by
        # flush time it shows the POST-scroll page. Grabbing the cached frame now
        # gives scroll the same pre-action observation every other action gets.
        if self.scroll_accum == 0:
            self._scroll_pre_png = self.recorder.last_png_bytes
        delta_y = ev.get("deltaY", 0)
        # Fara: positive = up, wheel.deltaY: positive = down
        self.scroll_accum -= int(round(delta_y))
        if self._scroll_task and not self._scroll_task.done():
            self._scroll_task.cancel()
        self._scroll_task = asyncio.create_task(self._scroll_flush_after(SCROLL_FLUSH_DELAY))

    async def _scroll_flush_after(self, delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._flush_scroll()

    # ---------- type debounce ----------
    # A typing burst self-commits after TYPE_FLUSH_DELAY of no keystrokes, so
    # the `type` action (and its thought card) appears even if the user never
    # follows it with a click/Enter.

    def _restart_type_timer(self):
        if self._type_task and not self._type_task.done():
            self._type_task.cancel()
        self._type_task = asyncio.create_task(self._type_flush_after(TYPE_FLUSH_DELAY))

    async def _type_flush_after(self, delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._flush_type()

    # ---------- backspace coalescing ----------
    # A run of standalone backspaces self-commits as ONE key action after
    # BACKSPACE_FLUSH_DELAY of no further backspaces (or sooner if another action
    # interrupts the run, via the _flush_backspace() calls in the handlers).

    def _restart_backspace_timer(self):
        if self._backspace_task and not self._backspace_task.done():
            self._backspace_task.cancel()
        self._backspace_task = asyncio.create_task(
            self._backspace_flush_after(BACKSPACE_FLUSH_DELAY))

    async def _backspace_flush_after(self, delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._flush_backspace()

    # ---------- flushers (caller must hold lock) ----------

    async def _flush_type(self):
        if not self.type_buffer:
            return
        entry = {"action": "type", "text": self.type_buffer}
        if self.type_coord is not None:
            entry["coordinate"] = self.type_coord
        # Use the cached frame (refreshed ~every 0.5s while typing) instead of a
        # live capture: the card then appears instantly instead of waiting on a
        # fromSurface screenshot. The cache already reflects the typed text.
        await self.recorder.record(entry, fresh=False)
        self.type_buffer = ""

    async def _flush_scroll(self):
        if self.scroll_accum == 0:
            return
        # Pair the scroll with the frame captured BEFORE the gesture started,
        # not the (post-scroll) background cache available now.
        await self.recorder.record(
            {"action": "scroll", "pixels": self.scroll_accum},
            png_override=self._scroll_pre_png)
        self.scroll_accum = 0
        self._scroll_pre_png = None

    async def _flush_backspace(self):
        if self.backspace_count <= 0:
            return
        n = self.backspace_count
        self.backspace_count = 0          # reset first: a stale debounce that
        entry = {"action": "key", "keys": ["Backspace"]}   # fires later no-ops
        if n > 1:
            entry["count"] = n            # how many times to press Backspace
        await self.recorder.record(entry)
