"""Per-step recorder: viewport screenshot via page.screenshot() + jsonl row.

Each entry:
{
  "timestamp": "...",
  "screenshot": "screenshot/0001.png",
  "viewport_size": [1428, 896],
  "action": { Fara-style action dict }
}

Screenshot is captured AT the moment the action handler runs, which is
slightly AFTER the action takes place (~50-150 ms). For practical web-agent
training this is usually fine; if you need pre-action screenshots, switch
to a memory-cached background capture (more complex).
"""

import json
import os
import shutil
import threading

from utils import get_current_time


class WebActionRecorder:
    def __init__(self, page, session_dir, task_description, viewport_size=(1428, 896)):
        self.page = page
        self.session_dir = session_dir
        self.task_description = task_description
        self.viewport_size = list(viewport_size)

        os.makedirs(session_dir, exist_ok=True)
        self.screenshot_dir = os.path.join(session_dir, "screenshot")
        os.makedirs(self.screenshot_dir, exist_ok=True)

        self.jsonl_path = os.path.join(session_dir, "trajectory.jsonl")
        self.meta_path = os.path.join(session_dir, "meta.json")
        self.step = 0

        # In-memory cache of the most recent screenshot bytes (captured at the
        # last user action). If the browser is closed via the X button before
        # terminate is recorded, we can still use this cache to save a final
        # PNG instead of just reusing the prior step's image.
        self.last_png_bytes = None

        # Optional callback fired right after each recorded action:
        #   card_callback(step:int, action_dict:dict, screenshot_abs_path:str)
        # The live thought popup uses this to show one card per action.
        self.card_callback = None
        # Guards concurrent jsonl writes between the asyncio recorder thread
        # (record) and the popup thread (set_thought, which rewrites a line).
        self._io_lock = threading.Lock()

        self._write_meta(start_time=get_current_time())

    def _write_meta(self, **fields):
        meta = {
            "task_description": self.task_description,
            "viewport_size": self.viewport_size,
            "total_steps": self.step,
        }
        meta.update(fields)
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def set_page(self, page):
        """Swap the page used for screenshots (e.g. after navigation)."""
        self.page = page

    async def record(self, action_dict, fresh=False):
        """Record one action. When fresh=True, take an immediate screenshot
        instead of using the background cache — used for `type` so the
        screenshot reflects the FULL typed text (cache may lag by up to 500ms
        and miss the last few keystrokes)."""
        # Wait actions reuse the previous screenshot — taking a fresh capture
        # adds a visible flash without showing anything new (the user was idle).
        is_wait = action_dict.get("action") == "wait"
        reuse_prev = is_wait and self.last_png_bytes is not None and self.step > 0

        if reuse_prev:
            png_bytes = None
            rel = f"screenshot/{self.step:04d}.jpg"  # reference previous step's file
        else:
            if fresh or self.last_png_bytes is None:
                try:
                    from tracker import cdp_screenshot
                    png_bytes = await cdp_screenshot(self.page, fmt="jpeg", quality=95)
                except Exception as e:
                    # The fresh capture fails when the action that triggered this
                    # flush also navigated the page — the classic case being the
                    # user typing a query and immediately pressing Enter: the
                    # Enter submits/navigates before we can screenshot. We used to
                    # `return` here, silently DROPPING the whole `type` action
                    # (so only the Enter survived, via its cached screenshot).
                    # Fall back to the last cached frame — that's the pre-Enter
                    # page, the correct image for the type — instead of losing it.
                    if self.last_png_bytes is not None:
                        print(f"  [warn] fresh screenshot failed, using cached frame: {e}")
                        png_bytes = self.last_png_bytes
                    else:
                        print(f"  [warn] screenshot failed, skipping action {action_dict}: {e}")
                        return
            else:
                # PRE-action cached screenshot — sidesteps the keydown/navigation race.
                png_bytes = self.last_png_bytes
            self.step += 1
            name = f"{self.step:04d}.jpg"
            rel = f"screenshot/{name}"
            with open(os.path.join(self.screenshot_dir, name), "wb") as f:
                f.write(png_bytes)
            self.last_png_bytes = png_bytes

        if reuse_prev:
            self.step += 1

        entry = {
            "timestamp": get_current_time(),
            "screenshot": rel,
            "viewport_size": self.viewport_size,
            "action": action_dict,
            "thought": "",
        }
        with self._io_lock:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        tag = "" if not reuse_prev else " (reused screenshot)"
        print(f"[{self.step:>3}] {action_dict}{tag}")

        # Notify the live thought popup — only when a real screenshot file was
        # written this step (skip the wait/reuse path, which references a prior
        # step's image).
        if self.card_callback and not reuse_prev:
            try:
                shot_abs = os.path.join(self.screenshot_dir, f"{self.step:04d}.jpg")
                self.card_callback(self.step, action_dict, shot_abs)
            except Exception as e:
                print(f"  [warn] card_callback failed: {e}")

        return self.step

    def set_thought(self, step, text):
        """Attach a thought to the row recorded at `step` (1-based) by
        rewriting that single jsonl line. Called from the popup thread;
        thread-safe with record() via _io_lock.

        Relies on the invariant that one recorded step == one jsonl line in
        order, so the line index is simply step-1."""
        text = (text or "").strip()
        with self._io_lock:
            if not os.path.isfile(self.jsonl_path):
                return
            try:
                with open(self.jsonl_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception:
                return
            idx = step - 1
            if idx < 0 or idx >= len(lines):
                return
            try:
                obj = json.loads(lines[idx])
            except Exception:
                return
            obj["thought"] = text
            lines[idx] = json.dumps(obj, ensure_ascii=False) + "\n"
            try:
                with open(self.jsonl_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            except Exception as e:
                print(f"  [warn] set_thought write failed: {e}")

    def finalize(self):
        self._write_meta(end_time=get_current_time())

    def discard(self):
        if os.path.isdir(self.session_dir):
            shutil.rmtree(self.session_dir)
            print(f"Discarded session: {self.session_dir}")
