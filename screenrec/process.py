"""Turn raw.mkv + events.json into a small, zoomed, captioned mp4 plus WhatsApp text."""
import json
import math
import os
import subprocess
import wave
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import describe, winutil

FONT = "C:/Windows/Fonts/segoeuib.ttf"


def _run(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=winutil.NO_WINDOW)
    if r.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed:\n{r.stderr[-1500:]}")
    return r.stdout


def _probe(path):
    out = _run([winutil.ffmpeg_exe("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json",
                str(path)])
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    return {"w": v["width"], "h": v["height"], "duration": float(info["format"]["duration"]),
            "v_start": float(v.get("start_time", 0)), "a_start": float(a["start_time"]) if a else None}


# ---------------- audio ----------------
def _extract_audio(raw, meta, out, rate, extra="", fill_gaps=False):
    d = meta["a_start"] - meta["v_start"]  # align audio to the video's first frame
    shift = f"adelay={int(d * 1000)}:all=1" if d > 0 else f"atrim=start={-d:.3f}"
    fill = "aresample=async=1," if fill_gaps else ""  # joined segments: pad gaps so audio stays in sync
    af = f"asetpts=PTS-STARTPTS,{fill}{shift},asetpts=PTS-STARTPTS{extra}"
    _run([winutil.ffmpeg_exe(), "-y", "-i", str(raw), "-map", "0:a:0", "-af", af, "-ac", "1", "-ar", str(rate),
          "-c:a", "pcm_s16le", str(out)])


def _read_wav(path):
    with wave.open(str(path)) as w:
        return w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def _write_wav(path, rate, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.astype(np.int16).tobytes())


# ---------------- analysis ----------------
def transcribe(wav16, cfg, progress):
    from faster_whisper import WhisperModel
    progress("Transcribing speech…")
    model = WhisperModel(cfg.whisper_model, device="cpu", compute_type="int8", cpu_threads=os.cpu_count())
    _, samples = _read_wav(wav16)  # hand over samples: faster-whisper's own decoder trips on newer PyAV
    segments, _ = model.transcribe(samples.astype(np.float32) / 32768, language=cfg.language or None,
                                   vad_filter=True, word_timestamps=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"start": w.start, "end": w.end, "word": w.word})
    return words


def motion_profile(raw, fps=5):
    """Fraction of changed pixels between consecutive low-res frames."""
    w, h = 320, 180
    p = subprocess.Popen([winutil.ffmpeg_exe(), "-v", "error", "-i", str(raw), "-vf",
                          f"setpts=PTS-STARTPTS,fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE, creationflags=winutil.NO_WINDOW)
    prev, i, out = None, 0, []
    while True:
        buf = p.stdout.read(w * h)
        if len(buf) < w * h:
            break
        cur = np.frombuffer(buf, np.uint8).astype(np.int16)
        if prev is not None:
            out.append((i / fps, float(np.count_nonzero(np.abs(cur - prev) > 12)) / cur.size))
        prev, i = cur, i + 1
    p.wait()
    return out


def keep_segments(duration, motion, words, ev, cfg, fps):
    if not cfg.trim_idle:
        return [(0.0, duration)]
    active = [(t - 0.2, t + 0.2) for t, frac in motion if frac > 0.0015]  # > ~cursor-sized change
    active += [(w["start"], w["end"]) for w in words]
    active += [(e["t"] - 0.3, e["t"] + 0.3) for e in ev["clicks"] + ev["keys"]]
    if not active:
        return [(0.0, duration)]
    pad = cfg.idle_pad
    spans = sorted((max(0.0, s - pad), min(duration, e + pad)) for s, e in active)
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s - merged[-1][1] <= cfg.min_idle_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    q = lambda t: round(t * fps) / fps  # keep cuts on the frame grid so audio stays in sync
    return [(q(s), q(e)) for s, e in merged if q(e) > q(s)]


class TimeMap:
    def __init__(self, segs):
        self.segs, self.offsets, acc = segs, [], 0.0
        for s, e in segs:
            self.offsets.append(acc)
            acc += e - s
        self.duration = acc

    def out(self, t):
        """Source time -> output time (snaps into the nearest following kept segment)."""
        for (s, e), off in zip(self.segs, self.offsets):
            if t < s:
                return off
            if t <= e:
                return off + t - s
        return self.duration

    def kept(self, t):
        return any(s <= t < e for s, e in self.segs)


# ---------------- zoom ----------------
def _ease(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


class Zoom:
    """Zoom in ahead of each burst of clicks, pan between them, ease back out afterwards."""

    def __init__(self, clicks, level):
        self.level, self.groups = level, []
        for c in sorted(clicks, key=lambda c: c["t"]):
            if self.groups and c["t"] - self.groups[-1][-1]["t"] < 3.0:
                self.groups[-1].append(c)
            else:
                self.groups.append([c])

    def at(self, t):
        for g in self.groups:
            t1, t2 = g[0]["t"], g[-1]["t"]
            if t1 - 0.9 <= t <= t2 + 2.1:
                env = _ease((t - (t1 - 0.9)) / 0.6) * (1 - _ease((t - (t2 + 1.5)) / 0.6))
                cx, cy = g[0]["x"], g[0]["y"]
                for prev, nxt in zip(g, g[1:]):
                    k = _ease((t - (nxt["t"] - 0.6)) / 0.5)
                    cx, cy = cx + (nxt["x"] - prev["x"]) * k, cy + (nxt["y"] - prev["y"]) * k
                return 1 + (self.level - 1) * env, cx, cy
        return 1.0, 0, 0


# ---------------- overlays ----------------
class Labels:
    def __init__(self, out_h):
        self.cache = {}
        self.cap_font = ImageFont.truetype(FONT, max(18, int(out_h * 0.042)))
        self.key_font = ImageFont.truetype(FONT, max(16, int(out_h * 0.036)))

    def render(self, text, font, max_w, bg):
        key = (text, id(font))
        if key in self.cache:
            return self.cache[key]
        words, lines, cur = text.split(), [], ""
        for w in words:
            test = f"{cur} {w}".strip()
            if font.getlength(test) > max_w and cur:
                lines.append(cur)
                cur = w
            else:
                cur = test
        lines.append(cur)
        asc, desc = font.getmetrics()
        lh, pad = asc + desc, int(font.size * 0.45)
        tw = int(max(font.getlength(l) for l in lines))
        img = Image.new("RGBA", (tw + 2 * pad, lh * len(lines) + pad), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=pad, fill=bg)
        for i, l in enumerate(lines):
            d.text(((img.width - font.getlength(l)) / 2, pad // 2 + i * lh), l, font=font, fill=(255, 255, 255))
        arr = np.array(img)[:, :, [2, 1, 0, 3]]  # RGBA -> BGRA
        self.cache[key] = arr
        return arr


def _blend(frame, img, x, y):
    h, w = img.shape[:2]
    x, y = max(0, x), max(0, y)
    h, w = min(h, frame.shape[0] - y), min(w, frame.shape[1] - x)
    if h <= 0 or w <= 0:
        return
    a = img[:h, :w, 3:4].astype(np.float32) / 255
    roi = frame[y:y + h, x:x + w]
    roi[:] = (roi * (1 - a) + img[:h, :w, :3] * a).astype(np.uint8)


def caption_chunks(words, max_words=7, max_len=2.8):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w["end"] - cur[0]["start"] > max_len
                    or w["start"] - cur[-1]["end"] > 0.7):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w["word"].strip().endswith((".", "?", "!")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    merged = []  # don't leave one or two words of a sentence stranded on their own caption
    for c in chunks:
        if merged and len(c) <= 2 and not merged[-1][-1]["word"].strip().endswith((".", "?", "!")) \
                and c[0]["start"] - merged[-1][-1]["end"] < 0.7:
            merged[-1] = merged[-1] + c
        else:
            merged.append(c)
    chunks, out = merged, []
    for i, c in enumerate(chunks):
        end = c[-1]["end"] + 0.4
        if i + 1 < len(chunks):
            end = min(end, chunks[i + 1][0]["start"])
        out.append({"start": c[0]["start"], "end": end, "text": "".join(w["word"] for w in c).strip()})
    return out


# ---------------- render + encode ----------------
def render(raw, meta, ev, segs, captions, cfg, out_path, progress):
    fps = cfg.out_fps
    W, H = meta["w"] - meta["w"] % 2, meta["h"] - meta["h"] % 2
    out_h = min(cfg.out_height, H)
    out_h -= out_h % 2
    out_w = int(round(W * out_h / H / 2)) * 2
    scale = out_w / W
    rx, ry = ev["region"][0], ev["region"][1]
    clicks = [dict(c, x=c["x"] - rx, y=c["y"] - ry) for c in ev["clicks"]]
    clicks = [c for c in clicks if 0 <= c["x"] < W and 0 <= c["y"] < H]
    zoom = Zoom([c for c in clicks if c["button"] == "left"], cfg.zoom if cfg.zoom_enabled else 1.0)
    labels = Labels(out_h)
    tm = TimeMap(segs)

    dec = subprocess.Popen([winutil.ffmpeg_exe(), "-v", "error", "-i", str(raw), "-vf",
                            f"setpts=PTS-STARTPTS,fps={fps},crop={W}:{H}:0:0", "-f", "rawvideo", "-pix_fmt",
                            "bgr24", "-"], stdout=subprocess.PIPE, creationflags=winutil.NO_WINDOW)
    enc = subprocess.Popen([winutil.ffmpeg_exe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{out_w}x{out_h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset",
                            "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", str(out_path)],
                           stdin=subprocess.PIPE, creationflags=winutil.NO_WINDOW)
    n, written, total = 0, 0, int(tm.duration * fps)
    frame_bytes = W * H * 3
    while True:
        buf = dec.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        t = n / fps
        n += 1
        if not tm.kept(t):
            continue
        src = np.frombuffer(buf, np.uint8).reshape(H, W, 3)

        z, cx, cy = zoom.at(t)
        cw, ch = W / z, H / z
        x0 = min(max(cx - cw / 2, 0), W - cw) if z > 1.001 else 0
        y0 = min(max(cy - ch / 2, 0), H - ch) if z > 1.001 else 0
        crop = src[int(y0):int(y0 + ch), int(x0):int(x0 + cw)]
        interp = cv2.INTER_AREA if crop.shape[1] > out_w else cv2.INTER_CUBIC
        frame = cv2.resize(crop, (out_w, out_h), interpolation=interp)
        k = out_w / cw

        if cfg.click_highlights:
            for c in clicks:
                age = t - c["t"]
                if 0 <= age <= 0.9:
                    px, py = int((c["x"] - x0) * k), int((c["y"] - y0) * k)
                    u = out_h / 720  # size relative to the output, so it reads on a phone
                    r = int((16 + 34 * _ease(age / 0.5)) * u)
                    over = frame.copy()
                    color = (0, 200, 255) if c["button"] == "left" else (255, 140, 60)
                    cv2.circle(over, (px, py), r, color, max(3, int(6 * u)), cv2.LINE_AA)
                    cv2.circle(over, (px, py), int(9 * u), color, -1, cv2.LINE_AA)
                    alpha = 0.9 * (1 - _ease((age - 0.4) / 0.5))
                    cv2.addWeighted(over, alpha, frame, 1 - alpha, 0, frame)

        if cfg.key_overlay:
            recent = [e for e in ev["keys"] if 0 <= t - e["t"] <= 1.3]
            if recent:
                img = labels.render(recent[-1]["label"], labels.key_font, out_w, (30, 30, 30, 220))
                _blend(frame, img, (out_w - img.shape[1]) // 2, int(out_h * 0.05))

        cap = next((c for c in captions if c["start"] <= t < c["end"]), None)
        if cap:
            img = labels.render(cap["text"], labels.cap_font, int(out_w * 0.8), (0, 0, 0, 170))
            _blend(frame, img, (out_w - img.shape[1]) // 2, out_h - img.shape[0] - int(out_h * 0.05))

        enc.stdin.write(frame.tobytes())
        written += 1
        if written % (fps * 10) == 0:
            progress(f"Rendering… {min(99, 100 * written // max(total, 1))}%")
    dec.stdout.close()
    dec.wait()
    enc.stdin.close()
    if enc.wait():
        raise RuntimeError("Render encode failed")
    return out_w, out_h, written / fps


def encode_final(render_path, audio, out, duration, cfg, workdir, progress):
    ff = winutil.ffmpeg_exe()
    inputs = ["-i", str(render_path)] + (["-i", str(audio)] if audio else [])
    audio_args = ["-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "48000"] if audio else ["-an"]
    common = ["-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0", "-g", str(cfg.out_fps * 10),
              "-movflags", "+faststart"]
    progress("Compressing…")
    _run([ff, "-y", *inputs, "-c:v", "libx264", "-preset", "slow", "-crf", "26", *common, *audio_args, str(out)])
    target = cfg.target_mb * 1024 * 1024
    if out.stat().st_size <= target:
        return
    # Too big: two-pass at the bitrate that lands just under the target.
    progress("Shrinking to fit WhatsApp…")
    kbps = int(target * 8 * 0.95 / duration / 1000) - (64 if audio else 0)
    vf = ["-vf", "scale=-2:540"] if kbps < 350 else []
    log = str(workdir / "x264pass")
    v = ["-c:v", "libx264", "-preset", "slow", "-b:v", f"{max(kbps, 80)}k", "-passlogfile", log]
    _run([ff, "-y", "-i", str(render_path), *vf, *v, "-pass", "1", *common, "-an", "-f", "mp4", "NUL"])
    _run([ff, "-y", *inputs, *vf, *v, "-pass", "2", *common, *audio_args, str(out)])
    for f in workdir.glob("x264pass*"):
        f.unlink()


def write_srt(captions, tm, path):
    def ts(s):
        ms = int(round(s * 1000))
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    lines = []
    for i, c in enumerate(captions, 1):
        lines += [str(i), f"{ts(tm.out(c['start']))} --> {ts(tm.out(c['end']))}", c["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def process_session(session: Path, cfg, progress=print, keep_text=False):
    """keep_text: leave an existing whatsapp.txt alone (re-making the video after the text was edited)."""
    session = Path(session)
    raw = session / "raw.mkv"
    ev = json.loads((session / "events.json").read_text(encoding="utf-8-sig"))
    meta = _probe(raw)
    fps = cfg.out_fps

    words, audio_cut = [], None
    if ev.get("has_audio") and meta["a_start"] is not None:
        progress("Preparing audio…")
        gaps = ev.get("segments", 1) > 1
        _extract_audio(raw, meta, session / "speech16k.wav", 16000, fill_gaps=gaps)
        _extract_audio(raw, meta, session / "audio48k.wav", 48000,
                       ",highpass=f=80,lowpass=f=12000,dynaudnorm=f=150:g=15", fill_gaps=gaps)
        if cfg.captions or cfg.trim_idle:
            words = transcribe(session / "speech16k.wav", cfg, progress)

    progress("Finding the idle parts…")
    motion = motion_profile(raw)
    segs = keep_segments(meta["duration"], motion, words, ev, cfg, fps)
    tm = TimeMap(segs)
    captions = caption_chunks(words) if cfg.captions else []

    if ev.get("has_audio") and meta["a_start"] is not None:
        rate, samples = _read_wav(session / "audio48k.wav")
        parts = [samples[int(s * rate):int(e * rate)] for s, e in segs]
        audio_cut = session / "audio_cut.wav"
        _write_wav(audio_cut, rate, np.concatenate(parts) if parts else samples[:0])

    progress("Rendering…")
    rendered = session / "render.mkv"
    _, _, duration = render(raw, meta, ev, segs, captions, cfg, rendered, progress)

    stamp = session.name
    video = session / f"howto_{stamp}.mp4"
    encode_final(rendered, audio_cut, video, duration, cfg, session, progress)
    srt = session / f"howto_{stamp}.srt"
    if captions:
        write_srt(captions, tm, srt)
    else:
        srt.unlink(missing_ok=True)
    # Kept so the review window can rewrite the message without redoing the whole video.
    (session / "analysis.json").write_text(json.dumps({"words": words, "segs": segs}), encoding="utf-8")

    text_path = session / "whatsapp.txt"
    if not (keep_text and text_path.exists()):
        progress("Writing the WhatsApp text…")
        text_path.write_text(describe.make_text(ev, words, tm, cfg), encoding="utf-8")

    for f in ("speech16k.wav", "audio48k.wav", "audio_cut.wav", "render.mkv"):
        (session / f).unlink(missing_ok=True)
    if not cfg.keep_raw:
        raw.unlink(missing_ok=True)

    result = {"video": video, "size_mb": video.stat().st_size / 1048576, "duration": duration,
              "raw_duration": meta["duration"], "segments": len(segs)}
    (session / "result.json").write_text(json.dumps({k: str(v) for k, v in result.items()}, indent=1),
                                         encoding="utf-8")
    return result
