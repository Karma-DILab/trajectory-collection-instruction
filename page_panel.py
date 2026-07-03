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
        label = "key  " + " + ".join(action.get("keys", []))
        n = action.get("count")
        if n and n > 1:
            label += f"  ×{n}"   # e.g. "key  Backspace  ×3"
        return label
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
        self.card_win = None   # separate-window card (set by tracker); the card
                               # lives OUT of the page so it can't break page nav

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
        # The card itself lives in a separate OS window that survives page
        # navigation, so a fresh page only needs to RE-APPLY the dim/lock if a
        # thought is still pending — it doesn't re-render the card.
        if self._current is not None:
            return {"pending": True}
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
            if self.card_win is not None:
                self.card_win.hide()
            try:                          # hand focus back to the browser window
                if self.page is not None:
                    await self.page.bring_to_front()
            except Exception:
                pass

    async def on_delete(self, step):
        """The card's delete button was pressed for `step`: drop that step from
        the trajectory entirely (jsonl line + screenshot) and renumber the rest,
        then advance to the next queued card or release the page.

        Mirrors on_thought's tail, but instead of saving a thought it removes
        the step. Because delete_step renumbers every LATER step down by one, we
        also fix the step number (and screenshot path) of any still-queued
        cards so their eventual thought/delete targets the right line."""
        try:
            step = int(step)
        except Exception:
            return
        ok = False
        try:
            ok = self.recorder.delete_step(step)
        except Exception as e:
            print(f"  [warn] delete_step failed: {e}")
        try:
            if self._current is not None and int(self._current["step"]) == step:
                self._current = None
        except Exception:
            self._current = None
        if ok:
            for c in self._queue:
                try:
                    if int(c["step"]) > step:
                        c["step"] = int(c["step"]) - 1
                        c["img_path"] = os.path.join(
                            self.recorder.screenshot_dir, f"{c['step']:04d}.jpg")
                except Exception:
                    pass
        if self._queue:
            self._current = self._queue.pop(0)   # promote next (sync)
            await self._show(self._current)
        else:
            await self._set_lock(False)   # nothing left -> release the page
            if self.card_win is not None:
                self.card_win.hide()
            try:                          # hand focus back to the browser window
                if self.page is not None:
                    await self.page.bring_to_front()
            except Exception:
                pass

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
        """Sync entry point from recorder.record(). Promotes the action to the
        current card SYNCHRONOUSLY (so pending_card sees it immediately) and
        schedules the async show. The card window loads the screenshot file
        itself, so we just pass the path."""
        try:
            self._queue.append({"step": step, "summary": summarize(action),
                                "img_path": shot_abs})
            if self._current is None and self._queue:
                self._current = self._queue.pop(0)
                asyncio.create_task(self._show(self._current))
        except Exception as e:
            print(f"  [warn] panel enqueue failed: {e}")

    async def _show(self, card):
        # Show the card FIRST — card_win.show is non-blocking (it just queues a
        # command to the window's own thread). THEN dim+block the page. If we
        # awaited the lock first, a navigating action's page.evaluate can stall
        # while the page changes, and the card would appear late / look like it
        # vanished (page goes dark, no card). The in-page dim is harmless — only
        # the old in-page card OVERLAY broke navigation; the card now lives in a
        # separate window that never touches the page DOM.
        if self.card_win is not None:
            try:
                self.card_win.show(card["step"], card["summary"],
                                   card.get("img_path"), self.task,
                                   self.pending_count())
            except Exception as e:
                print(f"  [warn] card window show failed: {e}")
        await self._set_lock(True)

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
