"""In-page thought panel driver (Python side).

The panel UI itself lives in inject.js (a card injected into the page). This
class feeds it cards and collects the thoughts back, replacing the old separate
Tk window — so there is exactly one OS window and no cross-window keyboard-focus
problem (focusing the thought box is just DOM focus in the same window).

Flow per action:
    recorder.record(...) -> card_callback == panel.enqueue(step, action, shot)
      -> schedules _pump() -> page.evaluate(__webtrack_showcard, card)
    user types in the in-page textarea, submits
      -> page calls __webtrack_thought(step, text) == panel.on_thought
      -> recorder.set_thought + show next queued card, or unlock the page
"""
import asyncio
import base64
import os


def summarize(action):
    """One-line label for an action dict (shown on the card)."""
    if not isinstance(action, dict):
        return str(action)
    a = action.get("action")
    if a == "left_click":
        c = action.get("coordinate") or ["?", "?"]
        return f"left_click ({c[0]}, {c[1]})"
    if a == "type":
        t = action.get("text", "")
        return 'type  "' + (t[:67] + "..." if len(t) > 70 else t) + '"'
    if a == "search":
        t = action.get("text", "")
        return 'search  "' + (t[:57] + "..." if len(t) > 60 else t) + '"  ⏎'
    if a == "scroll":
        return f"scroll  {action.get('pixels', 0):+d}px"
    if a == "key":
        return "key  " + " + ".join(action.get("keys", []))
    if a == "wait":
        return f"wait  {action.get('time', 0)}s"
    if a == "terminate":
        return f"terminate  ({action.get('status', '')})"
    return str(action)


class InPagePanel:
    def __init__(self, recorder, task="", on_finish=None):
        self.recorder = recorder
        self.task = task or ""
        self.on_finish = on_finish
        self._queue = []       # cards waiting to be shown: {step, summary, img}
        self._current = None   # card on screen, awaiting its thought
        self._processing = 0   # input events being handled right now (see below)
        self.closed = False
        self.outcome = None    # "success"/"fail"/"retry" chosen on the finish buttons

    @property
    def page(self):
        return self.recorder.page

    # ---------- state (read by tracker: bg-cache gate + teardown wait) ----------

    def pending_count(self):
        return len(self._queue) + (1 if self._current is not None else 0)

    # ---------- exposed to the page ----------

    def pending_card(self):
        """__webtrack_pending_card: what a freshly-loaded page should do.
            {step, ...}      -> re-show this unanswered card (stay locked)
            {"busy": True}   -> an action's event is still being recorded;
                                keep waiting (the card is about to appear)
            None             -> idle; release the page
        The busy state closes the race where a navigating action (e.g. a
        search) lands us on the new page before Python has finished recording
        it — without it the page would unlock and leak the next inputs."""
        c = self._current
        if c:
            return {**c, "task": self.task, "pending": self.pending_count()}
        if self._processing > 0:
            return {"busy": True}
        return None

    async def on_thought(self, step, text):
        """__webtrack_thought: a thought was submitted for `step`."""
        try:
            self.recorder.set_thought(int(step), text)
        except Exception as e:
            print(f"  [warn] set_thought failed: {e}")
        try:
            if self._current is not None and int(self._current["step"]) == int(step):
                self._current = None
        except Exception:
            self._current = None
        if self._queue:
            self._current = self._queue.pop(0)   # promote next (sync)
            await self._show(self._current)
        else:
            await self._set_lock(False)   # all answered -> release the page

    def finish(self, status="success"):
        """__webtrack_finish: one of the 성공/실패/다시하기 buttons was clicked.

        Records the chosen outcome (used as the terminate status), drops any
        in-progress card — the worker is ending now, so we don't want the
        terminate card stuck in a queue behind a half-finished one — then signals
        the stop. run_tracker's teardown records the terminate action with this
        outcome, whose card then appears for the final thought (판단 이유)."""
        s = str(status or "success").lower()
        if s not in ("success", "fail", "retry"):
            s = "success"
        self.outcome = s
        self.closed = True
        self._queue = []
        self._current = None
        if callable(self.on_finish):
            try:
                self.on_finish()
            except Exception as e:
                print(f"  [warn] on_finish failed: {e}")

    # ---------- card pump ----------

    def enqueue(self, step, action, shot_abs):
        """Sync entry point from recorder.record(). Reads the screenshot into a
        data URL, promotes it to the current card SYNCHRONOUSLY (so pending_card
        / a freshly-loaded page sees it immediately, with no task-tick gap), and
        schedules the async page render."""
        try:
            img = ""
            if shot_abs and os.path.isfile(shot_abs):
                with open(shot_abs, "rb") as f:
                    img = "data:image/jpeg;base64," + \
                          base64.b64encode(f.read()).decode("ascii")
            self._queue.append({"step": step, "summary": summarize(action), "img": img})
            if self._current is None and self._queue:
                self._current = self._queue.pop(0)
                asyncio.create_task(self._show(self._current))
        except Exception as e:
            print(f"  [warn] panel enqueue failed: {e}")

    async def _show(self, card):
        pg = self.page
        if pg is None:
            return
        payload = {**card, "task": self.task, "pending": self.pending_count()}
        try:
            await pg.evaluate(
                "(c) => window.__webtrack_showcard && window.__webtrack_showcard(c)",
                payload)
        except Exception:
            # Page is navigating/closing — the next page re-fetches the card via
            # pending_card() on load, so nothing is lost.
            pass

    async def _set_lock(self, on):
        pg = self.page
        if pg is None:
            return
        try:
            await pg.evaluate(
                "(on) => window.__webtrack_setlock && window.__webtrack_setlock(on)",
                bool(on))
        except Exception:
            pass

    async def close(self):
        await self._set_lock(False)
