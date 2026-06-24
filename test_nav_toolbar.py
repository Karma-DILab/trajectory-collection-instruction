r"""Standalone visibility test for the floating nav toolbar.

Run this directly to confirm the toolbar WINDOW shows up on your desktop,
independent of the whole tracker:

    .\.venv\Scripts\python.exe test_nav_toolbar.py
    (or just:  python test_nav_toolbar.py)

A small dark bar with [← 뒤로] [→ 앞으로] [↻ 새로고침] should appear at the
TOP-LEFT of your screen for ~12 seconds. Click a button -> it prints the
direction in this console. If you SEE the bar, the toolbar code works and the
real tracker just needs a full restart. If you do NOT see it, tell me what this
console prints.
"""

import asyncio
import threading
import time
import tkinter as tk

from nav_toolbar import build_nav_toolbar


def main():
    # A throwaway asyncio loop so button clicks have somewhere to dispatch to.
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    async def on_nav(direction):
        print(f"  [click] nav -> {direction}", flush=True)

    print("Opening the nav toolbar for ~12 seconds...", flush=True)
    print("Look at the TOP-LEFT corner of your screen.", flush=True)

    root = tk.Tk()
    root.withdraw()   # hidden master; only the toolbar Toplevel is shown
    build_nav_toolbar(root, loop, on_nav, width=320, height=46, x=0, y=0)

    def _close():
        print("Closing toolbar.", flush=True)
        root.destroy()
        loop.call_soon_threadsafe(loop.stop)

    root.after(12000, _close)
    root.mainloop()
    time.sleep(0.2)


if __name__ == "__main__":
    main()
