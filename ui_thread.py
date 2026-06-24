"""Single shared Tk interpreter/thread, reused for the ENTIRE lifetime of the
process across every tracking session.

Two theories were tested and ruled out empirically before landing on this
design (see the trajectory-collection debugging session that produced this
file for the actual repro scripts):

  1. Two SEPARATE per-thread tk.Tk() roots (nav toolbar + card window each
     spawning their own thread), torn down around the same moment a tracking
     session ends -- suspected first, but a SINGLE shared root with two
     Toplevels hit the exact same "Tcl_AsyncDelete: async handler deleted by
     the wrong thread" crash, so simultaneous teardown of two interpreters
     was not the trigger.
  2. Creating a FRESH tk.Tk() in a FRESH thread once per tracking session
     (one at a time, never concurrently with another) -- THIS is what
     actually triggers it: by the 3rd create/destroy cycle within one
     process, Tcl's thread-local notifier bookkeeping gets corrupted (most
     likely a stale TLS slot / native thread-ID getting reused by a brand
     new OS thread, colliding with leftover Tcl state from an
     already-finalized thread).

The fix that held up under 8+ repeated create/destroy cycles in one process:
create ONE tk.Tk() root ONCE, run ONE mainloop forever in ONE dedicated
thread, and have each tracking session build/destroy ONLY its own Toplevel
windows against that already-running root -- never create a second Tk()
interpreter or spawn a second Tk thread for the life of the process.
"""

import os
import queue
import sys
import threading
import traceback


def _diag(msg):
    """Print AND append to a flushed debug file -- stdout alone is too
    unreliable to debug this with (block-buffered when redirected, so prints
    from a background thread in a frozen exe may never appear before exit)."""
    print(msg)
    try:
        state_dir = os.environ.get("WEBTRACKER_STATE_DIR") or os.getcwd()
        with open(os.path.join(state_dir, "ui_thread_debug.log"), "a",
                  encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


class _SharedUI:
    def __init__(self):
        self._cmds = queue.Queue()
        self.root = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self):
        _diag(f"  [diag] UI thread starting. frozen={getattr(sys, 'frozen', False)} "
              f"PATH={os.environ.get('PATH', '')[:300]}")
        try:
            import tkinter as tk
        except Exception as e:
            _diag(f"  [warn] UI thread unavailable (tkinter missing): "
                  f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            self._ready.set()
            return
        _diag("  [diag] tkinter imported OK")
        try:
            root = tk.Tk()
            root.withdraw()   # hidden master; only Toplevels are ever shown
        except Exception as e:
            _diag(f"  [warn] UI thread could not start: "
                  f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            self._ready.set()
            return
        _diag("  [diag] tk.Tk() root created OK")
        self.root = root
        self._ready.set()

        def poll():
            try:
                while True:
                    fn = self._cmds.get_nowait()
                    try:
                        fn()
                    except Exception as e:
                        print(f"  [warn] UI thread command failed: {e}")
            except queue.Empty:
                pass
            root.after(30, poll)

        root.after(30, poll)
        try:
            root.mainloop()
        except Exception:
            pass

    def run(self, fn, timeout=5):
        """Run fn() ON the Tk thread and block the caller until it returns.
        Re-raises any exception fn() raised, on the calling thread."""
        if self.root is None:
            raise RuntimeError("UI thread failed to start")
        done = threading.Event()
        box = {}

        def wrapped():
            try:
                box["v"] = fn()
            except Exception as e:
                box["e"] = e
            done.set()

        self._cmds.put(wrapped)
        if not done.wait(timeout=timeout):
            raise TimeoutError("UI thread did not respond in time")
        if "e" in box:
            raise box["e"]
        return box.get("v")


_shared = None
_shared_lock = threading.Lock()


def get_shared():
    """Return the process-wide shared UI thread, creating it on first call.
    Deliberately never torn down for the life of the process -- see the
    module docstring for why a fresh tk.Tk() per session is unsafe."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = _SharedUI()
        return _shared
