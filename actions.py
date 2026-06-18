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


SCROLL_FLUSH_DELAY = 0.5  # seconds idle -> commit one scroll gesture
TYPE_FLUSH_DELAY = 1.5    # seconds idle -> commit one typing burst
WAIT_INTERVAL = 12.0      # idle seconds -> emit a `wait` action (when enabled)


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
        self._scroll_task = None
        self._type_task = None
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
            await self._flush_type()
            await self._flush_scroll()


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
            await self.recorder.record({"action": "wait", "time": int(delay)})
        # re-arm so consecutive idle periods emit more `wait`s
        self._reset_wait_timer()

    # ---------- handlers ----------

    async def _on_click(self, ev):
        if ev.get("button") != 0:
            return  # right/middle ignored
        await self._flush_type()
        await self._flush_scroll()
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
            self.type_buffer += key
            self._restart_type_timer()
            return

        # Backspace while typing -> backspace inside buffer
        if not non_shift_mods and key == "Backspace" and self.type_buffer:
            self.type_buffer = self.type_buffer[:-1]
            self._restart_type_timer()
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
        # type buffer so the trajectory stays in {type, scroll, type, ...} order
        # instead of having scrolls cut into a typing burst silently.
        await self._flush_type()
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
        await self.recorder.record({"action": "scroll", "pixels": self.scroll_accum})
        self.scroll_accum = 0
