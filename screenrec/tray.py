"""System-tray app: hotkey toggles recording, a floating bar pauses/restarts it, and a review window
opens when you stop. Processing runs in the background."""
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from pynput import keyboard

from . import config, winutil
from .overlay import UI
from .recorder import Recorder, hotkey_label
from .review import Jobs, Review


def _icon(color, paused=False):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 12, 60, 52), 8, fill=(40, 40, 40, 255))
    if paused:
        d.rectangle((22, 21, 29, 43), fill=color)
        d.rectangle((35, 21, 42, 43), fill=color)
    else:
        d.ellipse((20, 20, 44, 44), fill=color)
    return img


ICONS = {"idle": _icon((200, 200, 200, 255)), "recording": _icon((255, 59, 48, 255)),
         "paused": _icon((255, 179, 0, 255), paused=True), "processing": _icon((255, 179, 0, 255))}


class App:
    def __init__(self):
        self.cfg = config.load()
        self.state = "idle"
        self.rec = None
        self.overlay = None
        self.busy = threading.Lock()  # one start/stop/pause at a time (hotkey, menu and bar can race)
        self.mics = winutil.list_mics()
        self.hk = hotkey_label(self.cfg.hotkey)
        self.ui = UI()
        self.jobs = Jobs(on_done=self._done)
        self.review = Review(config.load, self.jobs)  # fresh each time, so config.json edits apply at once
        self.icon = pystray.Icon("ScreenRec", ICONS["idle"], "ScreenRec", menu=pystray.Menu(self._menu))

    # ---------- menu ----------
    def _menu(self):
        M = pystray.MenuItem
        idle = lambda _i: self.state == "idle"
        live = lambda _i: self.state in ("recording", "paused")
        mic_items = [M(name, self._set_mic(name), checked=lambda _i, n=name: self.cfg.mic == n, radio=True)
                     for name in self.mics]
        mic_items.append(M("No microphone", self._set_mic(None), checked=lambda _i: not self.cfg.mic, radio=True))
        yield M(f"Record full screen ({self.hk})", lambda: self._bg(self.start), enabled=idle, default=True)
        yield M("Record an area…", lambda: self._bg(self.start_region), enabled=idle)
        yield M(lambda _i: "Resume recording" if self.state == "paused" else "Pause recording",
                lambda: self._bg(self.toggle_pause), enabled=live)
        yield M(f"Stop and make video ({self.hk})", lambda: self._bg(self.stop), enabled=live)
        yield pystray.Menu.SEPARATOR
        yield M("Review recordings…", lambda: self.review.open())
        yield M("After recording, make", pystray.Menu(*[
            M(label, self._set_output(key), checked=lambda _i, k=key: (self.cfg.output or "video") == k, radio=True)
            for key, label in (("video", "Video"), ("document", "Document (steps + screenshots)"),
                               ("both", "Video and document"))]))
        yield M("Microphone", pystray.Menu(*mic_items))
        yield M("Open recordings folder", lambda: os.startfile(self._out_root()))
        yield M("Edit settings", lambda: os.startfile(config.CONFIG_PATH))
        yield pystray.Menu.SEPARATOR
        yield M("Quit", self.quit)

    @staticmethod
    def _bg(fn):
        threading.Thread(target=fn, daemon=True).start()

    def _set_mic(self, name):
        def f():
            self.cfg["mic"] = name
            config.save(self.cfg)
        return f

    def _set_output(self, key):
        def f():
            self.cfg["output"] = key
            config.save(self.cfg)
        return f

    def _out_root(self):
        p = Path(self.cfg.output_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _set_state(self, state, tip=None):
        self.state = state
        shown = "processing" if state == "idle" and self.jobs.active() else state
        self.icon.icon = ICONS[shown]
        self.icon.title = tip or ("ScreenRec – making a video…" if shown == "processing" else f"ScreenRec – {state}")
        self.icon.update_menu()

    # ---------- actions ----------
    def toggle(self):
        if self.state == "idle":
            self.start()
        else:
            self.stop()

    def start_region(self):
        r = self.ui.select_region()
        if r:
            time.sleep(0.3)
            self.start(r, area=True)

    def start(self, region=None, area=False):
        with self.busy:
            if self.state != "idle":
                return
            self.cfg = config.load()
            region = region or winutil.monitor_at(*winutil.cursor_pos())
            session = self._out_root() / datetime.now().strftime("%Y-%m-%d_%H%M%S")
            try:
                self.rec = Recorder(self.cfg, region, session)
                self.rec.start()
            except Exception as e:
                self.icon.notify(str(e)[:250], "Could not start recording")
                return
            self.overlay = self.ui.show_recording(self.rec, area, {
                "pause": self.toggle_pause, "resume": self.toggle_pause,
                "restart": self.restart, "stop": self.stop})
            self._set_state("recording", f"Recording… {self.hk} to stop")
        if self.cfg.mic and not self.rec.has_audio:
            self.icon.notify("Microphone failed to open – recording without sound.", "ScreenRec")

    def toggle_pause(self):
        with self.busy:
            if self.state not in ("recording", "paused"):
                return
            try:
                if self.state == "recording":
                    self.rec.pause()
                    self._set_state("paused", "Paused – use the bar to resume")
                else:
                    self.rec.resume()
                    self._set_state("recording", f"Recording… {self.hk} to stop")
            except Exception as e:
                self.icon.notify(str(e)[:250], "Could not resume recording")

    def restart(self):
        with self.busy:
            if self.state not in ("recording", "paused"):
                return
            try:
                self.rec.restart()
                self._set_state("recording", f"Recording… {self.hk} to stop")
            except Exception as e:
                self.icon.notify(str(e)[:250], "Could not restart recording")
                self._set_state("paused")

    def stop(self):
        with self.busy:
            if self.state not in ("recording", "paused"):
                return
            overlay, self.overlay = self.overlay, None
            if overlay:
                self.ui.call(overlay.close)
            try:
                session = self.rec.stop()
            except Exception as e:
                self._set_state("idle")
                self.icon.notify(str(e)[:250], "Recording failed")
                return
            self.jobs.start(session, self.cfg)
            self._set_state("idle")
        self.review.open(session.name)

    def _done(self, session, result, st):
        if result and "video" in result:
            self.icon.notify(f"{result['size_mb']:.1f} MB, {result['duration']:.0f}s. "
                             "Check the message in the review window, then copy both into WhatsApp.",
                             "Video ready")
        elif result and "guide" in result:
            self.icon.notify(f"{result['steps']} steps with screenshots. Check it over in the review window.",
                             "Document ready")
        else:
            self.icon.notify(st["message"][:200], "That didn't work (see the review window)")
        self._set_state(self.state)

    def quit(self):
        if self.state in ("recording", "paused"):
            if self.overlay:
                self.ui.call(self.overlay.close)
            try:
                self.rec.stop()
            except Exception:
                pass
        self.hotkeys.stop()
        self.icon.stop()

    def run(self):
        # Hotkey callbacks run inside the keyboard hook: hand off to a thread immediately.
        self.hotkeys = keyboard.GlobalHotKeys({self.cfg.hotkey: lambda: self._bg(self.toggle)})
        self.hotkeys.start()

        def ready(icon):
            icon.visible = True
            icon.notify(f"Press {self.hk} to start and stop recording. Right-click this icon for options.",
                        "ScreenRec is ready")

        self.icon.run(setup=ready)


def already_running():
    """Named mutex: a second copy would answer the same hotkey and record twice."""
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\ScreenRecTray")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main():
    winutil.set_dpi_aware()
    if already_running():
        winutil.user32.MessageBoxW(None, "ScreenRec is already running – look for its icon by the clock "
                                         "(click ^ if hidden).", "ScreenRec", 0x40)
        return
    App().run()
