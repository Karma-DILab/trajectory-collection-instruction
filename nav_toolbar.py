"""Floating navigation toolbar (back / forward / reload) for the web tracker.

Built as a Toplevel of the process-lifetime shared root (see ui_thread.py)
instead of owning its own tk.Tk()/thread per session — recreating a fresh Tk
interpreter in a fresh thread every session corrupted Tcl's thread-local
notifier bookkeeping after a few sessions in one process.

It floats in its OWN top-level OS window, parked in a slim strip ABOVE the
tracked Chromium window. Being a separate window is the whole point:

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
"""

import asyncio


# (glyph, korean label, direction) for each button, left-to-right.
_BUTTONS = (
    ("←", "뒤로",     "back"),     # ←
    ("→", "앞으로",   "forward"),  # →
    ("↻", "새로고침", "reload"),   # ↻
)


def build_nav_toolbar(master, loop, on_nav, *, width, height=46, x=0, y=0):
    """Build the floating nav toolbar as a Toplevel of `master`.

    MUST be called ON the Tk thread that owns `master` (pass as a step inside
    a ui_thread.get_shared().run(...) call). Returns the Toplevel; it is torn
    down automatically when `master` (the shared root) is destroyed.

    loop:   the asyncio loop running the tracker (for run_coroutine_threadsafe)
    on_nav: coroutine function on_nav(direction) -> records + navigates
    width:  bar width in physical px (the process is DPI-aware, so Tk geometry
            px line up with Chrome's --window-size/position px)
    """
    import tkinter as tk

    win = tk.Toplevel(master)
    win.overrideredirect(True)               # no title bar / borders
    win.attributes("-topmost", True)         # float above the browser
    win.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
    win.configure(bg="#202124")

    def fire(direction):
        try:
            asyncio.run_coroutine_threadsafe(on_nav(direction), loop)
        except Exception as e:
            print(f"  [warn] nav dispatch failed: {e}")

    bar = tk.Frame(win, bg="#202124")
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
    win.update_idletasks()
    try:
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.update()
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
            hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
            cur = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, cur | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass
    _no_activate()

    def _tick():
        try:
            win.attributes("-topmost", True)   # re-assert if something stole it
        except Exception:
            return   # window already destroyed -> stop rescheduling
        win.after(250, _tick)
    win.after(250, _tick)

    return win
