"""Review window: a small local web app to watch a recording, fix its WhatsApp message and copy both.

Listens on 127.0.0.1 only. Every request must carry this run's token and the exact Host, so other web
pages (or DNS-rebinding tricks) can't drive it.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import claude_link, config, describe, winutil

PAGE = Path(__file__).with_name("review.html")
SESSION_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
REMAKE_KEYS = ("trim_idle", "zoom_enabled", "captions", "click_highlights", "key_overlay")
APP_BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe"]


def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default if default is not None else {}


def _title(text):
    for line in text.splitlines():
        line = re.sub(r"^how to:?\s*", "", line.strip().strip("*_~ ").strip(), flags=re.I)
        if line:
            return line[:1].upper() + line[1:]
    return ""


def _srt_to_vtt(srt):
    return "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", srt)


def recycle(path):
    """Send a folder to the Recycle Bin rather than deleting it outright."""
    ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; $p = @($input)[0]; "
          "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p, 'OnlyErrorDialogs', 'SendToRecycleBin')")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=str(path), text=True,
                   encoding="utf-8", capture_output=True, creationflags=winutil.NO_WINDOW)
    if path.exists():
        raise RuntimeError("Could not move the recording to the Recycle Bin (is the video open somewhere?)")


def send_to_whatsapp(video, text, cfg):
    """Open Claude Code with the send ready to go; you approve it there.

    The WhatsApp plugin only sends through its own tools, with a person approving each send to a group,
    so this can't happen silently in the background."""
    claude = shutil.which("claude.cmd") or shutil.which("claude.exe")
    if not claude:
        raise RuntimeError("Claude Code (claude) was not found on PATH")
    # The plugin only sends files from the literal home folders, not the redirected Documents.
    out = Path.home() / "Documents" / "ScreenRec" / "Sent"
    out.mkdir(parents=True, exist_ok=True)
    copy = out / video.name
    shutil.copy2(video, copy)
    caption = out / f"{video.stem}.caption.txt"
    caption.write_text(text.strip(), encoding="utf-8")
    # Kept free of double quotes and percent signs: it goes through cmd.exe via claude.cmd.
    prompt = (f"Post a new ScreenRec tutorial to my WhatsApp group, using the whatsapp skill. "
              f"The group chatId is {cfg.whatsapp_chat_id}; its last known title is {cfg.whatsapp_chat_name}, "
              f"so look up its current exact title with wa_chats first. Send the video file {copy} "
              f"with the caption being the full contents of {caption}, word for word - it is my own text, "
              f"already reviewed. Show me the group and the caption, call wa_send, and I will approve it in "
              f"the prompt. Then tell me whether it went out.")
    subprocess.Popen([claude, prompt], cwd=out, creationflags=0x10)  # CREATE_NEW_CONSOLE


class Jobs:
    """Background processing, one video at a time (they all compete for the CPU)."""

    def __init__(self, on_done=None):
        self.state = {}
        self.lock = threading.Lock()
        self.on_done = on_done

    def active(self, name=None):
        live = {n for n, s in self.state.items() if s["state"] in ("queued", "processing")}
        return name in live if name else bool(live)

    def start(self, session, cfg, keep_text=False, output=None):
        """output: "video", "document" or "both" (default: the output setting)."""
        name = session.name
        if self.active(name):
            return False
        self.state[name] = {"state": "queued", "message": "Waiting for the previous one to finish…"}
        output = output or cfg.output or "video"
        threading.Thread(target=self._run, args=(session, cfg, keep_text, output), daemon=True).start()
        return True

    def _run(self, session, cfg, keep_text, output):
        st = self.state[session.name]
        progress = lambda m: st.update(message=m)
        result = None
        with self.lock:
            st.update(state="processing", message="Starting…")
            try:
                result = {}
                if output in ("video", "both"):
                    from .process import process_session
                    result = process_session(session, cfg, progress=progress, keep_text=keep_text)
                if output in ("document", "both"):
                    from .document import make_guide
                    result.update(make_guide(session, cfg, progress=progress))
                (session / "error.txt").unlink(missing_ok=True)
                st.update(state="done", message="")
            except Exception as e:
                (session / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
                st.update(state="failed", message=str(e)[:400])
        if self.on_done:
            self.on_done(session, result, st)


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows reuse lets two servers share a port and steal requests


class Review:
    def __init__(self, get_cfg, jobs):
        self.get_cfg, self.jobs = get_cfg, jobs
        self.token = secrets.token_urlsafe(16)
        self.server = None
        for port in list(range(8765, 8775)) + [0]:
            try:
                self.server = _Server(("127.0.0.1", port), self._handler())
                break
            except OSError:
                continue
        self.server.daemon_threads = True
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True, name="review").start()

    # ---------- opening ----------
    def url(self, name=None):
        return f"http://{self.host}/?t={self.token}" + (f"#{name}" if name else "")

    def open(self, name=None):
        url = self.url(name)
        for exe in APP_BROWSERS:
            if Path(exe).exists():
                subprocess.Popen([exe, f"--app={url}", "--window-size=1320,880"], creationflags=winutil.NO_WINDOW)
                return
        webbrowser.open(url)

    # ---------- data ----------
    def root(self):
        return Path(self.get_cfg().output_dir)

    def session(self, name):
        if not SESSION_NAME.match(name or ""):
            raise LookupError(name)
        p = self.root() / name
        if not (p / "events.json").exists():
            raise LookupError(name)
        return p

    def info(self, session):
        name = session.name
        job = self.jobs.state.get(name)
        video = next(session.glob("howto_*.mp4"), None)
        srt = next(session.glob("howto_*.srt"), None)
        text_path = session / "whatsapp.txt"
        text = text_path.read_text(encoding="utf-8-sig") if text_path.exists() else ""
        result = _read_json(session / "result.json")
        review = _read_json(session / "review.json")
        guide_path = session / "guide" / "guide.md"
        guide = guide_path.read_text(encoding="utf-8-sig") if guide_path.exists() else None
        title = _title(text)
        if not title and guide:
            title = _title(guide.lstrip("# "))
        error = ""
        if job and job["state"] in ("queued", "processing", "failed"):
            state, message = job["state"], job["message"]
            error = message if state == "failed" else ""
        elif (session / "error.txt").exists():
            lines = (session / "error.txt").read_text(encoding="utf-8", errors="replace").strip().splitlines()
            state, message, error = "failed", "", lines[-1] if lines else "Unknown error"
        elif video or guide is not None:
            state, message = "done", ""
        else:
            state, message = "new", ""
        num = lambda k: float(result[k]) if k in result else None
        return {"name": name, "title": title, "state": state, "message": message, "error": error,
                "guide": guide if state == "done" else None,
                "video": video.name if video and state == "done" else None, "captions": bool(srt),
                "text": text, "duration": num("duration"), "raw_duration": num("raw_duration"),
                "size_mb": num("size_mb"), "has_raw": (session / "raw.mkv").exists(),
                "confirmed": bool(review.get("confirmed")), "can_rewrite": (session / "analysis.json").exists(),
                "whatsapp_to": self.get_cfg().whatsapp_chat_name if self.get_cfg().whatsapp_chat_id else None,
                "whatsapp_requested": review.get("whatsapp_requested")}

    def list(self):
        root = self.root()
        if not root.exists():
            return []
        out = []
        for p in sorted(root.iterdir(), reverse=True):
            if p.is_dir() and SESSION_NAME.match(p.name) and (p / "events.json").exists():
                i = self.info(p)
                item = {k: i[k] for k in ("name", "title", "state", "message", "duration", "confirmed")}
                item["kinds"] = [k for k in ("video", "guide") if i[k]]
                out.append(item)
        return out

    def settings(self, body):
        """Pick who writes documents: Claude (through Claude Code) or the local model."""
        if body.get("writer") in ("claude", "ollama"):
            cfg = config.load()
            cfg["guide_writer"] = body["writer"]
            config.save(cfg)
        return claude_link.status(self.get_cfg())

    def _mark(self, session, **kw):
        path = session / "review.json"
        data = _read_json(path)
        data.update(kw)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def action(self, session, act, body):
        text_path = session / "whatsapp.txt"
        video = next(session.glob("howto_*.mp4"), None)
        if act == "save":
            text_path.write_text(body["text"], encoding="utf-8")
            self._mark(session, confirmed=False)
        elif act == "confirm":
            text_path.write_text(body["text"], encoding="utf-8")
            describe.to_clipboard(body["text"])
            self._mark(session, confirmed=True)
        elif act == "copy-video":
            if not video:
                raise RuntimeError("There's no video yet")
            describe.file_to_clipboard(video)
        elif act == "whatsapp":
            if not video:
                raise RuntimeError("There's no video yet")
            text_path.write_text(body["text"], encoding="utf-8")
            send_to_whatsapp(video, body["text"], self.get_cfg())
            self._mark(session, confirmed=True, whatsapp_requested=time.strftime("%Y-%m-%d %H:%M"))
        elif act == "folder":
            subprocess.Popen(["explorer", "/select,", str(video)] if video else ["explorer", str(session)])
        elif act == "rewrite":
            from .process import TimeMap
            a = _read_json(session / "analysis.json")
            ev = _read_json(session / "events.json")
            text = describe.make_text(ev, a.get("words", []), TimeMap([tuple(s) for s in a["segs"]]),
                                      self.get_cfg())
            return {"text": text}
        elif act == "remake":
            if not (session / "raw.mkv").exists():
                raise RuntimeError("The original recording (raw.mkv) was not kept, so it can't be re-made")
            cfg = config.Config(self.get_cfg())
            cfg.update({k: bool(v) for k, v in body.get("options", {}).items() if k in REMAKE_KEYS})
            (session / "error.txt").unlink(missing_ok=True)
            output = body.get("output") if body.get("output") in ("video", "document", "both") else "video"
            self.jobs.start(session, cfg, keep_text=not body.get("rewrite_text"), output=output)
        elif act == "save-guide":
            (session / "guide" / "guide.md").write_text(body["md"], encoding="utf-8")
        elif act == "copy-guide":
            describe.to_clipboard((session / "guide" / "guide.md").read_text(encoding="utf-8"))
        elif act == "open-guide":
            os.startfile(session / "guide" / "guide.md")
        elif act == "guide-folder":
            subprocess.Popen(["explorer", str(session / "guide")])
        elif act == "delete":
            if self.jobs.active(session.name):
                raise RuntimeError("Wait for the video to finish first")
            recycle(session)
            self.jobs.state.pop(session.name, None)
        else:
            raise LookupError(act)
        return {"ok": True}

    # ---------- HTTP ----------
    def _handler(review):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _allowed(self, query):
                if self.headers.get("Host") != review.host:
                    return False
                given = self.headers.get("X-Token") or query.get("t", [""])[0]
                return secrets.compare_digest(given, review.token)

            def _json(self, data, code=200):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _bytes(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _file(self, path, ctype):
                size = path.stat().st_size
                start, end, code = 0, size - 1, 200
                m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:
                        start = max(0, size - int(m.group(2)))
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Cache-Control", "no-store")
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(left, 1 << 20))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)

            def _route(self, method):
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if not self._allowed(query):
                    return self._json({"error": "forbidden"}, 403)
                parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
                try:
                    if method == "GET" and not parts:
                        html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", review.token)
                        return self._bytes(html.encode(), "text/html; charset=utf-8")
                    if method == "GET" and parts == ["api", "list"]:
                        return self._json(review.list())
                    if parts == ["api", "settings"]:
                        if method == "POST":
                            n = int(self.headers.get("Content-Length") or 0)
                            return self._json(review.settings(json.loads(self.rfile.read(n) or b"{}")))
                        return self._json(claude_link.status(review.get_cfg()))
                    if parts[:2] == ["api", "rec"] and len(parts) >= 3:
                        session = review.session(parts[2])
                        if method == "GET" and len(parts) == 3:
                            return self._json(review.info(session))
                        if method == "POST" and len(parts) == 4:
                            n = int(self.headers.get("Content-Length") or 0)
                            body = json.loads(self.rfile.read(n) or b"{}")
                            return self._json(review.action(session, parts[3], body))
                    if method == "GET" and parts[:1] == ["media"] and parts[2:4] == ["guide", "images"] \
                            and len(parts) == 5 and re.match(r"^step-\d+\.png$", parts[4]):
                        img = review.session(parts[1]) / "guide" / "images" / parts[4]
                        if img.exists():
                            return self._file(img, "image/png")
                    if method == "GET" and parts[:1] == ["media"] and len(parts) == 3:
                        session = review.session(parts[1])
                        if parts[2] == "captions.vtt":
                            srt = next(session.glob("howto_*.srt"), None)
                            vtt = _srt_to_vtt(srt.read_text(encoding="utf-8")) if srt else "WEBVTT\n"
                            return self._bytes(vtt.encode(), "text/vtt; charset=utf-8")
                        video = session / parts[2]
                        if re.match(r"^howto_[\w-]+\.mp4$", parts[2]) and video.exists():
                            return self._file(video, "video/mp4")
                    return self._json({"error": "not found"}, 404)
                except LookupError:
                    return self._json({"error": "not found"}, 404)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass
                except Exception as e:
                    try:
                        return self._json({"error": str(e)[:400]}, 500)
                    except OSError:
                        pass

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")
        return H
