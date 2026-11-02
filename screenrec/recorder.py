"""Captures the screen region + mic with ffmpeg and logs clicks, shortcuts and pages visited.

Pausing ends the current ffmpeg segment; resuming starts a new one. On stop the segments are joined
into raw.mkv and every event time is on that joined timeline."""
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from pynput import keyboard, mouse

from . import winutil

K = keyboard.Key
MODS = {K.ctrl: "Ctrl", K.ctrl_l: "Ctrl", K.ctrl_r: "Ctrl", K.alt: "Alt", K.alt_l: "Alt", K.alt_r: "Alt",
        K.alt_gr: "Alt", K.shift: "Shift", K.shift_l: "Shift", K.shift_r: "Shift",
        K.cmd: "Win", K.cmd_l: "Win", K.cmd_r: "Win"}
SPECIAL = {K.enter: "Enter", K.esc: "Esc", K.tab: "Tab", K.delete: "Delete", K.f5: "F5", K.f11: "F11",
           K.f2: "F2", K.f12: "F12"}
MOD_ORDER = ("Ctrl", "Alt", "Shift", "Win")


def hotkey_label(spec):
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win"}
    return " + ".join(names.get(p.strip("<>"), p.strip("<>").upper()) for p in spec.split("+"))


class Recorder:
    def __init__(self, cfg, region, session_dir: Path):
        self.cfg = cfg
        x, y, w, h = region
        self.region = (x, y, w - w % 2, h - h % 2)
        self.dir = session_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.clicks, self.keys, self.windows = [], [], []
        self.mods = set()
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.url_poke = threading.Event()
        self.click_q = queue.Queue()
        self.ignore_label = hotkey_label(cfg.hotkey)
        self.has_audio = False
        self.encoder = self.mic = self.proc = None
        self.t0 = None
        self.segments = []     # finished segment files
        self.offset = 0.0      # seconds recorded in finished segments
        self.paused = False
        self.seg_lock = threading.Lock()

    # ---------- ffmpeg ----------
    def _cmd(self, encoder, mic, path):
        x, y, w, h = self.region
        cmd = [winutil.ffmpeg_exe(), "-hide_banner", "-nostats", "-y",
               "-thread_queue_size", "1024", "-f", "gdigrab", "-framerate", str(self.cfg.capture_fps),
               "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}", "-draw_mouse", "1",
               "-i", "desktop"]
        if mic:
            cmd += ["-thread_queue_size", "1024", "-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={mic}"]
        if encoder == "h264_qsv":
            cmd += ["-c:v", "h264_qsv", "-global_quality", "20", "-look_ahead", "0"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p"]
        if mic:
            cmd += ["-c:a", "pcm_s16le"]
        return cmd + [str(path)]

    def _launch(self, encoder, mic, path):
        proc = subprocess.Popen(self._cmd(encoder, mic, path), stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=winutil.NO_WINDOW)
        started = threading.Event()
        log = []

        def pump():
            for line in iter(proc.stderr.readline, b""):
                text = line.decode("utf-8", "replace").rstrip()
                log.append(text)
                if "Press [q]" in text and not started.is_set():
                    self.t0 = time.perf_counter()
                    started.set()

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.time() + 12
        while time.time() < deadline and not started.is_set() and proc.poll() is None:
            time.sleep(0.05)
        if started.is_set():
            time.sleep(0.3)  # an encoder that cannot init fails right after the banner
            if proc.poll() is None:
                self.log = log
                return proc
        proc.kill()
        raise RuntimeError("\n".join(log[-15:]))

    def _start_segment(self):
        path = self.dir / f"raw_{len(self.segments):03}.mkv"
        if self.encoder:  # later segments must match the first, or they cannot be joined
            attempts = [(self.encoder, self.mic)]
        else:
            mic = self.cfg.mic
            attempts = [(self.cfg.capture_encoder, mic), ("libx264", mic)]
            if mic:
                attempts.append(("libx264", None))  # last resort: keep recording without audio
        err = None
        for enc, m in attempts:
            try:
                self.proc = self._launch(enc, m, path)
                self.encoder, self.mic, self.has_audio = enc, m, bool(m)
                self.cur = path
                return
            except RuntimeError as e:
                err = e
        raise RuntimeError(f"ffmpeg could not start:\n{err}")

    def _end_segment(self):
        proc, self.proc = self.proc, None
        wall = time.perf_counter() - self.t0
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
            proc.wait()
        self.offset += winutil.media_duration(self.cur) or wall
        self.segments.append(self.cur)

    def _join(self):
        raw = self.dir / "raw.mkv"
        if len(self.segments) == 1:
            self.segments[0].replace(raw)
            return
        listing = self.dir / "segments.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in self.segments), encoding="utf-8")
        r = subprocess.run([winutil.ffmpeg_exe(), "-v", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(listing), "-c", "copy", str(raw)],
                           capture_output=True, text=True, creationflags=winutil.NO_WINDOW)
        if r.returncode:
            raise RuntimeError(f"Could not join the paused parts:\n{r.stderr[-800:]}")
        for p in self.segments:
            p.unlink(missing_ok=True)
        listing.unlink(missing_ok=True)

    def start(self):
        self._start_segment()
        self.mouse_l = mouse.Listener(on_click=self._on_click)
        self.key_l = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.mouse_l.start()
        self.key_l.start()
        self.sampler = threading.Thread(target=self._sample_windows, daemon=True)
        self.sampler.start()
        self.namer = threading.Thread(target=self._name_clicks, daemon=True)
        self.namer.start()

    def pause(self):
        with self.seg_lock:
            if self.paused or not self.proc:
                return
            self.paused = True  # stop logging input right away
            self._end_segment()

    def resume(self):
        with self.seg_lock:
            if not self.paused:
                return
            self._start_segment()
            self.paused = False

    def restart(self):
        """Throw away everything recorded so far and start again."""
        with self.seg_lock:
            self.paused = True
            if self.proc:
                self._end_segment()
            for p in self.segments:
                p.unlink(missing_ok=True)
            with self.lock:
                self.clicks, self.keys, self.windows = [], [], []
            self.segments, self.offset = [], 0.0
            self._start_segment()
            self.paused = False

    def elapsed(self):
        running = self.proc and not self.paused and self.t0
        return self.offset + (time.perf_counter() - self.t0 if running else 0.0)

    def stop(self):
        self.stop_flag.set()
        self.mouse_l.stop()
        self.key_l.stop()
        with self.seg_lock:
            if self.proc:
                self._end_segment()
            self.paused = True
        self.sampler.join(timeout=3)
        self.namer.join(timeout=3)
        if not self.segments:
            raise RuntimeError("Nothing was recorded")
        self._join()
        data = {"region": self.region, "has_audio": self.has_audio, "encoder": self.encoder,
                "segments": len(self.segments),
                "clicks": self.clicks, "keys": self.keys, "windows": self.windows}
        (self.dir / "events.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        return self.dir

    # ---------- input hooks (keep these fast) ----------
    def _now(self):
        return round(self.offset + time.perf_counter() - self.t0, 3)

    def _on_click(self, x, y, button, pressed):
        if pressed and not self.paused and button in (mouse.Button.left, mouse.Button.right):
            click = {"t": self._now(), "x": x, "y": y, "button": button.name}
            with self.lock:
                self.clicks.append(click)
            self.click_q.put(click)
            self.url_poke.set()

    def _on_press(self, key):
        if key in MODS:
            self.mods.add(MODS[key])
            return
        if self.paused:
            return
        mods = [m for m in MOD_ORDER if m in self.mods]
        if isinstance(key, K):
            name = SPECIAL.get(key) or (key.name.replace("_", " ").title() if mods else None)
        else:
            vk = getattr(key, "vk", None)
            name = chr(vk) if vk and (48 <= vk <= 57 or 65 <= vk <= 90) else (key.char or "").upper() or None
        if not name:
            return
        if {"Ctrl", "Alt", "Win"} & self.mods:
            label = " + ".join(mods + [name])
        elif key in SPECIAL:  # Enter/Esc/Tab alone (with optional Shift); plain typing is never logged
            label = " + ".join(mods + [name])
        else:
            return
        if label != self.ignore_label:
            with self.lock:
                self.keys.append({"t": self._now(), "label": label})
            if name in ("Enter", "F5") or label in ("Ctrl + L", "Ctrl + T", "Ctrl + W", "Ctrl + Tab"):
                self.url_poke.set()

    def _on_release(self, key):
        if key in MODS:
            self.mods.discard(MODS[key])

    # ---------- page / window tracking ----------
    def _name_clicks(self):
        """What was clicked ("Manage", a button), so a written guide can say so. Off the input hook."""
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            while not (self.stop_flag.is_set() and self.click_q.empty()):
                try:
                    click = self.click_q.get(timeout=0.3)
                except queue.Empty:
                    continue
                target = winutil.element_at(click["x"], click["y"])
                if target:
                    with self.lock:
                        click["target"] = target

    def _sample_windows(self):
        import uiautomation as auto
        last_title, last = None, None
        with auto.UIAutomationInitializerInThread():
            while not self.stop_flag.is_set():
                poked = self.url_poke.wait(0.6)
                if poked:
                    self.url_poke.clear()
                    time.sleep(0.8)  # let the page / address bar update
                try:
                    hwnd, title, exe = winutil.foreground()
                except Exception:
                    continue
                if self.paused or not title or exe in ("python.exe", "pythonw.exe") or (title == last_title and not poked):
                    continue
                last_title = title
                url = winutil.browser_url(hwnd) if exe in winutil.BROWSERS else None
                entry = (title, url)
                if entry != last:
                    last = entry
                    with self.lock:
                        self.windows.append({"t": self._now(), "title": title, "app": exe, "url": url})
