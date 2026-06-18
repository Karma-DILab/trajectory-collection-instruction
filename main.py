"""CLI entry for web_tracker_playwright.

Assigns a task from the predefined list (tasks_data.py) instead of asking the
worker to type one. The worker just performs the assigned task; thoughts are
collected per-action in the docked side panel.

Usage:
    .\\.venv\\Scripts\\python.exe main.py            # assign a random task
    .\\.venv\\Scripts\\python.exe main.py 5          # assign task with id 5
    .\\.venv\\Scripts\\python.exe main.py --list     # print all tasks and exit
"""

import asyncio
import os
import random
import sys
from datetime import datetime

from tracker import run_tracker
from tasks_data import TASKS


def _pick_task(argv):
    """Return the task dict to run, or None. Supports `main.py <id-or-index>`."""
    if not TASKS:
        return None
    if len(argv) > 1:
        arg = argv[1].strip()
        try:
            n = int(arg)
        except ValueError:
            print(f"  [warn] '{arg}' is not a number -> assigning a random task.")
            return random.choice(TASKS)
        for t in TASKS:                       # match by id first
            if t.get("id") == n:
                return t
        if 0 <= n < len(TASKS):               # fall back to list index
            return TASKS[n]
        print(f"  [warn] no task id/index {n} -> assigning a random task.")
    return random.choice(TASKS)


async def amain():
    print("=" * 60)
    print(" Web Tracker (Playwright) - viewport-only, Fara-style")
    print("=" * 60)

    if not TASKS:
        print("ERROR: no tasks loaded from tasks_data.py.")
        return

    if len(sys.argv) > 1 and sys.argv[1] in ("--list", "-l"):
        for t in TASKS:
            print(f"  [{t['id']:>3}] ({t.get('task_type','?'):<20}) {t['task']}")
        return

    chosen = _pick_task(sys.argv)
    if not chosen:
        print("ERROR: could not assign a task.")
        return

    task = chosen["task"]
    print(f"\nAssigned task  id={chosen['id']}  type={chosen.get('task_type','?')}")
    print(f"  -> {task}")

    # Fixed starting page (matches server mode).
    url = "https://www.google.com/?hl=en"   # hl=en forces Google's English UI

    here = os.path.dirname(os.path.abspath(__file__))
    events_root = os.path.join(here, "events")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(events_root, f"session_{ts}_task{chosen['id']:03d}")

    print(f"\nStarting Playwright Chromium...")
    print(f"Session dir: {session_dir}")

    try:
        recorder = await run_tracker(task, session_dir, url)
    except Exception as e:
        print(f"\nERROR during tracking: {type(e).__name__}: {e}")
        return

    # Auto-save (the console Enter used to stop already consumed stdin, so we
    # don't prompt again — discard empty sessions automatically).
    if recorder.step == 0:
        print("\nNo actions recorded. Discarding empty session.")
        recorder.discard()
        return

    # Record which predefined task this was (matches server-mode meta.json).
    try:
        import json
        meta_path = os.path.join(session_dir, "meta.json")
        meta = {}
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        meta["task_id"] = chosen["id"]
        meta["task_type"] = chosen.get("task_type")
        meta["site_url"] = chosen.get("site_url")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [warn] could not augment meta.json: {e}")

    print(f"\nSaved {recorder.step} actions: {session_dir}")


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        # A lingering input() thread (if the browser was closed instead of
        # pressing Enter) would block normal exit; force-exit cleanly.
        sys.stdout.flush()
        os._exit(0)
