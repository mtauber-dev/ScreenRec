"""On-screen UI while recording: floating control bar and a frame around the recorded area.

Tk lives on one thread of its own; other threads hand it work through UI.call(). Both overlays are
excluded from screen capture, so they never show up in the video.
"""
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import Future

from . import winutil

BG, FG, HOVER, MUTED = "#1f1f1f", "#ffffff", "#3a3a3a", "#9a9a9a"
RED, AMBER = "#ff3b30", "#ffb300"
FRAME_W = 3


class UI:
    def __init__(self):
        self.q = queue.Queue()
        ready = threading.Event()
        threading.Thread(target=self._loop, args=(ready,), daemon=True, name="tk").start()
        ready.wait()

    def _loop(self, ready):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.after(30, self._poll)
        ready.set()
        self.root.mainloop()

    def _poll(self):
        self.root.after(30, self._poll)
        while True:
            try:
                fn, fut = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                fut.set_result(fn())
            except Exception as e:
                fut.set_exception(e)

    def call(self, fn):
        """Run fn on the Tk thread and return its result. Never call this from the Tk thread itself."""
        fut = Future()
        self.q.put((fn, fut))
        return fut.result()

    def select_region(self):
        from .region import select_region
        return self.call(lambda: select_region(self.root))

    def show_recording(self, rec, area, actions):
        return self.call(lambda: RecordingOverlay(self.root, rec, area, actions))


def _prepare(win, click_through=False):
    win.update_idletasks()
    hwnd = int(win.wm_frame(), 16)
    winutil.make_overlay(hwnd, click_through)
    winutil.exclude_from_capture(hwnd)


class RecordingOverlay:
    """actions: dict of callables 'pause', 'resume', 'restart', 'stop' (run off the Tk thread)."""

    def __init__(self, root, rec, area, actions):
        self.root, self.rec, self.actions = root, rec, actions
        self.busy = False
        self.confirm_restart = None
        x, y, w, h = rec.region
        self.mon = winutil.monitor_at(x + w // 2, y + h // 2)
        self.frame = self._make_frame(rec.region) if area else []
        self._make_bar(rec.region, area)
        self._tick()

    # ---------- frame around the area ----------
    def _make_frame(self, region):
        x, y, w, h = region
        t = FRAME_W
        mx, my, mw, mh = self.mon
        edges = [(x - t, y - t, w + 2 * t, t), (x - t, y + h, w + 2 * t, t),
                 (x - t, y, t, h), (x + w, y, t, h)]
        wins = []
        for ex, ey, ew, eh in edges:
            # Keep each edge on screen; it may then overlap the area, but it's hidden from capture.
            ex = min(max(ex, mx), mx + mw - ew) if ew < mw else mx
            ey = min(max(ey, my), my + mh - eh) if eh < mh else my
            win = tk.Toplevel(self.root, bg=RED)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.geometry(f"{ew}x{eh}+{ex}+{ey}")
            _prepare(win, click_through=True)
            wins.append(win)
        return wins

    # ---------- control bar ----------
    def _make_bar(self, region, area):
        bar = self.bar = tk.Toplevel(self.root, bg=BG)
        bar.overrideredirect(True)
        bar.attributes("-topmost", True)
        bar.attributes("-alpha", 0.96)

        grip = tk.Label(bar, text="⋮⋮", bg=BG, fg=MUTED, font=("Segoe UI", 11), cursor="fleur", padx=6)
        grip.pack(side="left", fill="y")
        self.dot = tk.Label(bar, text="●", bg=BG, fg=RED, font=("Segoe UI", 12))
        self.dot.pack(side="left", padx=(2, 4))
        self.clock = tk.Label(bar, text="0:00", bg=BG, fg=FG, font=("Segoe UI Semibold", 11), width=6,
                              anchor="w", cursor="fleur")
        self.clock.pack(side="left")
        self.pause_btn = self._button(bar, "❚❚  Pause", self._toggle_pause)
        self.restart_btn = self._button(bar, "↺  Restart", self._restart)
        self._button(bar, "■  Finish", lambda: self._run("stop"), fg=RED)
        for wdg in (grip, self.clock, self.dot):
            wdg.bind("<ButtonPress-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag)

        bar.update_idletasks()
        bw, bh = bar.winfo_reqwidth(), bar.winfo_reqheight()
        x, y, w, h = region
        mx, my, mw, mh = self.mon
        gap = 10
        if area and y - bh - gap >= my:
            by = y - bh - gap                      # just above the area
        elif area and y + h + bh + gap <= my + mh - 48:
            by = y + h + gap                       # just below it
        else:
            by = my + mh - bh - 64                 # bottom of the screen, above the taskbar
        bx = min(max(x + (w - bw) // 2, mx + 8), mx + mw - bw - 8)
        bar.geometry(f"+{bx}+{by}")
        _prepare(bar)

    def _button(self, parent, text, cmd, fg=FG):
        b = tk.Label(parent, text=text, bg=BG, fg=fg, font=("Segoe UI", 10), padx=10, pady=7, cursor="hand2")
        b.pack(side="left", padx=1)
        b.bind("<Enter>", lambda _e: b.config(bg=HOVER))
        b.bind("<Leave>", lambda _e: b.config(bg=BG))
        b.bind("<ButtonRelease-1>", lambda _e: cmd())
        return b

    def _drag_start(self, e):
        self._drag_off = (e.x_root - self.bar.winfo_x(), e.y_root - self.bar.winfo_y())

    def _drag(self, e):
        ox, oy = self._drag_off
        self.bar.geometry(f"+{e.x_root - ox}+{e.y_root - oy}")

    # ---------- actions ----------
    def _run(self, name):
        """Recorder calls block for a moment (ffmpeg stopping/starting): keep them off the Tk thread."""
        if self.busy:
            return
        self.busy = True
        self.bar.config(cursor="watch")

        def work():
            try:
                self.actions[name]()
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()

    def _toggle_pause(self):
        self._run("resume" if self.rec.paused else "pause")

    def _restart(self):
        # Two clicks within 3 s, so a stray click can't throw the recording away.
        if self.confirm_restart and time.monotonic() - self.confirm_restart < 3:
            self.confirm_restart = None
            self._run("restart")
        else:
            self.confirm_restart = time.monotonic()
            self.restart_btn.config(text="↺  Click again", fg=AMBER)

    def _tick(self):
        if not self.bar.winfo_exists():
            return
        paused = self.rec.paused
        s = int(self.rec.elapsed())
        self.clock.config(text=f"{s // 60}:{s % 60:02}" if not paused else "Paused",
                          fg=FG if not paused else AMBER)
        blink = paused and int(time.monotonic() * 2) % 2
        self.dot.config(fg=AMBER if paused else RED, text="" if blink else "●")
        self.pause_btn.config(text="▶  Resume" if paused else "❚❚  Pause")
        if self.confirm_restart and time.monotonic() - self.confirm_restart >= 3:
            self.confirm_restart = None
        if not self.confirm_restart:
            self.restart_btn.config(text="↺  Restart", fg=FG)
        self.bar.config(cursor="watch" if self.busy else "")
        for f in self.frame:
            f.config(bg=AMBER if paused else RED)
        # Other always-on-top windows can cover us; stay in front.
        for w in self.frame + [self.bar]:
            w.lift()
        self.bar.after(250, self._tick)

    def close(self):
        for w in self.frame + [self.bar]:
            w.destroy()
