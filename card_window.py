"""Thought-card window — a Toplevel of the shared UI-thread root (see
ui_thread.py) that shows the per-action card (screenshot, action, thought
textarea) OUTSIDE the tracked page.

Why separate: an in-page overlay card, when revealed over the page, cancels
the page's OWN JS click action on some sites (Amazon "Buy Now", hover-dropdown
navigation just don't fire). A separate OS window never touches the page DOM, so
it can't interfere. The in-page dim + input-block stay (harmless); only the card
moved out here.

Cross-thread: the panel (asyncio loop) pushes commands onto a queue; the
shared Tk thread (ui_thread.get_shared()) drains it via root.after. Submit
schedules the panel's on_thought coroutine back on the loop via
run_coroutine_threadsafe.
"""

import asyncio
import os
import queue


class CardWindow:
    def __init__(self, loop, on_thought):
        self.loop = loop
        self.on_thought = on_thought          # coroutine: on_thought(step, text)
        self._cmds = queue.Queue()
        self._cur_step = None
        self._photo = None                    # keep a ref so Tk doesn't GC it
        self.win = None                       # the Toplevel, once built

    # ---------------- build (MUST run on the shared Tk thread) ---------------

    def build(self, master, *, width=460, height=640, x=0, y=0):
        """Build the card as a Toplevel of `master`. Call this via
        ui_thread.get_shared().run(...), never from the asyncio thread
        directly — Tk widgets must be created on the thread that owns the
        interpreter."""
        import tkinter as tk
        from PIL import Image, ImageTk

        win = tk.Toplevel(master)
        self.win = win
        win.title("생각 입력")
        win.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
        win.configure(bg="#ffffff")
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass

        wrap = width - 28
        pad = {"padx": 14, "fill": "x"}

        task_lbl = tk.Label(win, text="", bg="#ffffff", fg="#8a8f98",
                            font=("Segoe UI", 9), wraplength=wrap,
                            justify="left", anchor="w")
        task_lbl.pack(**pad, pady=(12, 2))

        tk.Label(win, text="관찰 (스크린샷)", bg="#ffffff", fg="#0a7d4b",
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(**pad)
        img_lbl = tk.Label(win, bg="#f3f4f6", text="(스크린샷 대기...)",
                           fg="#8a8f98")
        img_lbl.pack(**pad, pady=(2, 6))

        tk.Label(win, text="행동", bg="#ffffff", fg="#1a73e8",
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(**pad)
        act_lbl = tk.Label(win, text="", bg="#f3f4f6", fg="#202124",
                           font=("Consolas", 10), anchor="w", justify="left",
                           wraplength=wrap, padx=8, pady=6)
        act_lbl.pack(**pad, pady=(2, 6))

        tk.Label(win, text="생각  (직접 작성, 필수)", bg="#ffffff", fg="#7b2ff7",
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(**pad)
        ta = tk.Text(win, height=5, font=("Segoe UI", 11), wrap="word",
                     bd=1, relief="solid")
        ta.pack(**pad, pady=(2, 6))

        status = tk.Label(win, text="", bg="#ffffff", fg="#8a8f98",
                          font=("Segoe UI", 9), anchor="e")

        def do_submit():
            text = ta.get("1.0", "end").strip()
            if not text:
                ta.focus_set()
                return
            step = self._cur_step
            if step is None:
                return
            save_btn.config(state="disabled")
            status.config(text="저장 중...")
            try:
                asyncio.run_coroutine_threadsafe(
                    self.on_thought(step, text), self.loop)
            except Exception as e:
                print(f"  [warn] thought dispatch failed: {e}")

        save_btn = tk.Button(win, text="확인  (Enter)", command=do_submit,
                             bg="#1a73e8", fg="#ffffff",
                             font=("Segoe UI", 11, "bold"), bd=0, relief="flat",
                             cursor="hand2")
        save_btn.pack(**pad, pady=(0, 4))
        status.pack(**pad, pady=(0, 8))

        def on_return(e):
            if e.state & 0x0001:      # Shift held -> allow a newline
                return
            do_submit()
            return "break"           # plain Enter -> submit, no newline
        ta.bind("<Return>", on_return)

        def render(step, summary, img_path, task, pending):
            self._cur_step = step
            task_lbl.config(text="Task: " + (task or ""))
            act_lbl.config(text=summary or "")
            status.config(text="남은 입력: " + str(pending or 1))
            save_btn.config(state="normal")
            ta.delete("1.0", "end")
            try:
                if img_path and os.path.isfile(img_path):
                    im = Image.open(img_path)
                    h = int(im.height * (wrap / im.width))
                    if h > 300:
                        h = 300
                    w2 = int(im.width * (h / im.height))
                    im = im.resize((max(1, w2), max(1, h)), Image.LANCZOS)
                    self._photo = ImageTk.PhotoImage(im)
                    img_lbl.config(image=self._photo, text="")
                else:
                    img_lbl.config(image="", text="(스크린샷 없음)")
            except Exception:
                img_lbl.config(image="", text="(스크린샷 로드 실패)")
            try:
                win.deiconify()
                win.lift()
                win.attributes("-topmost", True)
                ta.focus_force()
            except Exception:
                pass

        def poll():
            try:
                while True:
                    cmd, arg = self._cmds.get_nowait()
                    if cmd == "show":
                        render(*arg)
                    elif cmd == "hide":
                        try:
                            win.withdraw()
                        except Exception:
                            pass
            except queue.Empty:
                pass
            try:
                win.after(60, poll)
            except Exception:
                pass   # window already destroyed -> stop rescheduling

        win.after(60, poll)

    # ---------------- thread-safe API (called from the asyncio thread) -------

    def show(self, step, summary, img_path, task, pending):
        self._cmds.put(("show", (step, summary, img_path, task, pending)))

    def hide(self):
        self._cmds.put(("hide", None))
