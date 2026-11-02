"""Document mode: turn a narrated recording into a step-by-step Markdown guide with screenshots.

Each click (or shortcut, or new page) becomes a step with a screenshot taken just before it, the
click marked in red. The narration around each step is what the step text is written from: a local
Ollama model interprets it into clear instructions; without one, the narration itself is used.

Writes <session>/guide/guide.md with images in guide/images, so the folder can be moved as a whole.
"""
import json
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from . import claude_link, describe, winutil
from .process import _extract_audio, _probe, transcribe

MAX_W = 1600          # screenshot width in the guide
LEAD = 1.0            # narration usually comes just before the action it describes
MAX_STEPS = 40


# ---------------- steps ----------------
def _window_at(ev, t):
    last = None
    for w in ev.get("windows", []):
        if w["t"] <= t + 0.5:
            last = w
    return last


def find_steps(ev, duration):
    rx, ry, w, h = ev["region"]
    steps = []
    for c in sorted(ev["clicks"], key=lambda c: c["t"]):
        x, y = c["x"] - rx, c["y"] - ry
        if c["button"] != "left" or not (0 <= x < w and 0 <= y < h):
            continue
        prev = steps[-1] if steps else None
        if prev and c["t"] - prev["t_last"] < 1.0 and abs(prev["x"] - x) + abs(prev["y"] - y) < 60:
            prev["t_last"], prev["double"] = c["t"], True   # double-click: one step
            continue
        steps.append({"kind": "click", "t": c["t"], "t_last": c["t"], "x": x, "y": y, "target": c.get("target")})
    for k in ev["keys"]:
        near = [s for s in steps if 0 <= k["t"] - s["t_last"] < 1.5]
        if near:   # a shortcut right after a click belongs to that click
            near[-1].setdefault("keys", []).append(k["label"])
        else:
            steps.append({"kind": "key", "t": k["t"], "t_last": k["t"], "keys": [k["label"]]})
    # A new page with no click around it (typed an address, a link opened by itself): a step of its own.
    seen = set()
    for win in ev.get("windows", []):
        key = (win.get("url") or win["title"]).split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        if win["t"] > 1 and not any(-3 < s["t"] - win["t"] < 3 for s in steps):
            steps.append({"kind": "page", "t": win["t"], "t_last": win["t"]})
    steps.sort(key=lambda s: s["t"])
    while len(steps) > MAX_STEPS:  # very long recordings: fold the closest pair together
        i = min(range(len(steps) - 1), key=lambda i: steps[i + 1]["t"] - steps[i]["t"])
        steps[i].setdefault("keys", []).extend(steps[i + 1].get("keys", []))
        del steps[i + 1]
    for s in steps:
        s["window"] = _window_at(ev, s["t"])
        s["shot_t"] = min(max(0.0, s["t"] + 1.5 if s["kind"] == "page" else s["t"] - 0.3), duration - 0.1)
    return steps


def _phrases(words):
    """Sentences (or stretches between long pauses), so narration is never cut mid-sentence."""
    out, cur = [], []
    for w in words:
        if cur and w["start"] - cur[-1]["end"] > 0.8:
            out.append(cur)
            cur = []
        cur.append(w)
        if w["word"].strip().endswith((".", "?", "!")):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"start": p[0]["start"], "text": "".join(w["word"] for w in p).strip()} for p in out]


def attach_narration(steps, words):
    """Each step gets the sentences that start from just before it until just before the next one."""
    bounds = [s["t"] - LEAD for s in steps] + [float("inf")]
    phrases = _phrases(words)
    for i, s in enumerate(steps):
        s["said"] = " ".join(p["text"] for p in phrases if bounds[i] <= p["start"] < bounds[i + 1])
    return " ".join(p["text"] for p in phrases if p["start"] < bounds[0])


# ---------------- screenshots ----------------
def _frame(raw, t, w, h):
    out = subprocess.run(
        [winutil.ffmpeg_exe(), "-v", "error", "-ss", f"{t:.3f}", "-i", str(raw), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, creationflags=winutil.NO_WINDOW)
    if len(out.stdout) < w * h * 3:
        return None
    return np.frombuffer(out.stdout[:w * h * 3], np.uint8).reshape(h, w, 3).copy()


def screenshots(raw, meta, steps, img_dir, progress):
    img_dir.mkdir(parents=True, exist_ok=True)
    for f in img_dir.glob("step-*.png"):
        f.unlink()
    w, h = meta["w"], meta["h"]
    scale = min(1.0, MAX_W / w)
    prev = None
    for i, s in enumerate(steps, 1):
        if i % 5 == 0:
            progress(f"Taking screenshots… {i}/{len(steps)}")
        frame = _frame(raw, s["shot_t"], w, h)
        if frame is None:
            continue
        small = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 \
            else frame
        same = prev is not None and float(np.mean(cv2.absdiff(small, prev))) < 0.4
        prev = small.copy()
        if same and s["kind"] != "click":
            continue  # nothing changed on screen: the step reads fine without a repeat picture
        if s["kind"] == "click":
            c = (int(s["x"] * scale), int(s["y"] * scale))
            r = max(16, int(26 * small.shape[1] / 1280))
            overlay = small.copy()
            cv2.circle(overlay, c, r, (48, 59, 255), -1, cv2.LINE_AA)
            cv2.addWeighted(overlay, 0.25, small, 0.75, 0, small)
            cv2.circle(small, c, r, (48, 59, 255), max(3, r // 7), cv2.LINE_AA)
        name = f"step-{i:02}.png"
        cv2.imwrite(str(img_dir / name), small, [cv2.IMWRITE_PNG_COMPRESSION, 6])
        s["image"] = f"images/{name}"


# ---------------- interpretation ----------------
def _action(s):
    win = s.get("window") or {}
    where = describe.clean_title(win.get("title", "")) or "the screen"
    if s["kind"] == "click":
        tg = s.get("target")
        thing = f"the {tg['kind']} \"{tg['name']}\"" if tg else "something"
        what = f"{'double-clicked' if s.get('double') else 'clicked'} {thing} in \"{where}\""
    elif s["kind"] == "page":
        what = f"opened \"{where}\""
    else:
        what = f"pressed a key in \"{where}\""
    if s.get("keys"):
        what += f", then pressed {', '.join(s['keys'])}"
    return what


def _ollama_json(cfg, prompt):
    describe._ensure_ollama(cfg)
    body = json.dumps({"model": cfg.ollama_model, "prompt": prompt, "stream": False, "format": "json",
                       "options": {"temperature": 0.2, "num_ctx": 4096}}).encode()
    req = urllib.request.Request(cfg.ollama_url.rstrip("/") + "/api/generate", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(json.loads(r.read())["response"])


def _parse_json(reply):
    m = re.search(r"\{.*\}", reply, re.S)  # tolerate words around the JSON
    if not m:
        raise RuntimeError("Claude gave no JSON")
    return json.loads(m.group(0))


def _api_json(client, prompt, steps, gdir):
    """Ask Claude through the API, with each step's screenshot attached (shrunk to save tokens)."""
    import base64
    content = [{"type": "text", "text": prompt}]
    for i, s in enumerate(steps, 1):
        if not s.get("image"):
            continue
        img = cv2.imread(str(gdir / s["image"]))
        if img.shape[1] > 1280:
            img = cv2.resize(img, (1280, int(img.shape[0] * 1280 / img.shape[1])), interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        content += [{"type": "text", "text": f"Screenshot for step {i}:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.standard_b64encode(jpg.tobytes()).decode()}}]
    response = client.beta.messages.create(
        model=claude_link.MODEL, max_tokens=16000, output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",  # another model answers if one declines
        messages=[{"role": "user", "content": content}])
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this guide")
    return _parse_json("".join(b.text for b in response.content if b.type == "text"))


def _claude_json(prompt, gdir):
    """Ask Claude Code (headless) for the guide. It may only read files: the screenshots."""
    claude = claude_link.claude_code()
    if not claude:
        raise RuntimeError("Claude Code not found")
    r = subprocess.run([claude, "-p", "--output-format", "json", "--allowedTools", "Read",
                        "--disallowedTools", "Bash,Edit,Write,WebFetch,WebSearch"],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=gdir, timeout=900, creationflags=winutil.NO_WINDOW)
    if r.returncode:
        raise RuntimeError(f"claude failed: {(r.stderr or r.stdout)[-400:]}")
    return _parse_json(json.loads(r.stdout).get("result", ""))


def interpret(cfg, intro, steps, gdir):
    """Writer, best first: Claude via a connected API key, Claude Code, then the local Ollama model."""
    want_claude = (cfg.guide_writer or "claude") == "claude"
    client = claude_link.api_client() if want_claude else None
    use_cli = want_claude and not client and claude_link.claude_code()
    lines = "\n".join(
        f"{i}. Action: {_action(s)}. Said: \"{s['said'] or '(nothing)'}\""
        + (f" Screenshot: {gdir / s['image']}" if use_cli and s.get("image") else "")
        for i, s in enumerate(steps, 1))
    where = "Open the screenshots with the Read tool" if use_cli else "Look at the screenshots attached below"
    look = f"""
Each screenshot was taken just before the action; a red circle marks where the click landed. {where}
and use them to name exactly what was clicked or typed into (the button, link, tab, menu item or field
label you can read there).
""" if client or use_cli else ""
    # The narration goes in once, split over the steps: the whole transcript again doubles the time.
    prompt = f"""You turn a narrated screen recording into a written step-by-step guide.

What was said before the first action: \"{intro or '(nothing)'}\"

The recording's steps, in order. Each has what the narrator did and what they said around that moment:
{lines}
{look}

Return JSON only, in this shape:
{{"title": "How to ...", "intro": "...", "steps": [{{"n": 1, "text": "...", "skip": false}}], "tips": ["..."]}}

Rules:
- Write in the same language as the narration.
- title: short, starting with "How to" (or the same in the narration's language).
- intro: one or two sentences on what this guide is for, taken from the narration.
- One entry in "steps" for every numbered step above, same n. "text": one or two clear sentences
  telling the reader what to do, starting with a verb. Explain why only when the narrator did.
  Never write "clicked something": say what to click, using the name in the action when there is one,
  otherwise what the narration says. Put names of buttons, links, tabs and fields in **bold**.
- "skip": true for a step that was clearly a stray click with nothing said and no purpose.
- tips: warnings or useful remarks from the narration that are not steps. Empty list if none.
- Never invent buttons, names or steps that the narration, window titles or screenshots don't support."""
    data = None
    try:
        if client:
            data = _api_json(client, prompt, steps, gdir)
        elif use_cli:
            data = _claude_json(prompt, gdir)
    except Exception:
        pass  # fall back to the local model
    if data is None:
        data = _ollama_json(cfg, prompt)
    texts = {int(s.get("n", 0)): s for s in data.get("steps", []) if isinstance(s, dict)}
    for i, s in enumerate(steps, 1):
        got = texts.get(i, {})
        text = str(got.get("text") or "").strip()
        s["text"] = "" if re.search(r"\bclick(ed)? something\b", text, re.I) else text  # parroted, not written
        s["skip"] = bool(got.get("skip")) and not s["said"]
    return str(data.get("title") or "").strip(), str(data.get("intro") or "").strip(), \
        [str(t).strip() for t in data.get("tips", []) if str(t).strip()]


def _fallback_text(s):
    if s["said"]:
        return s["said"]
    win = describe.clean_title((s.get("window") or {}).get("title", ""))
    if s["kind"] == "page":
        return f"Open *{win}*." if win else "Open the next page."
    keys = f" and press **{' + '.join(s['keys'])}**" if s.get("keys") and s["kind"] == "click" else ""
    if s["kind"] == "key":
        return f"Press **{', '.join(s['keys'])}**."
    tg = s.get("target")
    if tg:
        return f"Click the **{tg['name']}** {tg['kind']}{keys}."
    return f"Click as shown{f' in *{win}*' if win else ''}{keys}."


# ---------------- markdown ----------------
def to_markdown(title, intro, steps, tips, links, transcript, when, quotes=True):
    md = [f"# {title}", ""]
    if intro:
        md += [intro, ""]
    shown = [s for s in steps if not s.get("skip")]
    md += [f"*Recorded {when} · {len(shown)} steps*", "", "## Steps", ""]
    for n, s in enumerate(shown, 1):
        md += [f"### {n}. {s['text']}", ""]
        if s.get("image"):
            md += [f"![Step {n}]({s['image']})", ""]
        if quotes and s["said"] and s["said"] != s["text"]:
            md += [f"> “{s['said']}”", ""]
    if tips:
        md += ["## Tips", ""] + [f"- {t}" for t in tips] + [""]
    if links:
        md += ["## Links", ""] + [f"- [{p['title'] or p['url']}]({p['url']})" for p in links] + [""]
    if transcript:
        md += ["<details>", "<summary>Full transcript</summary>", "", transcript, "", "</details>", ""]
    return "\n".join(md)


class _Identity:
    def out(self, t):
        return t


def make_guide(session: Path, cfg, progress=print):
    session = Path(session)
    raw = session / "raw.mkv"
    ev = json.loads((session / "events.json").read_text(encoding="utf-8-sig"))
    meta = _probe(raw)
    gdir = session / "guide"
    gdir.mkdir(exist_ok=True)

    # Reuse the video's transcript when there is one; otherwise transcribe here.
    words = json.loads((session / "analysis.json").read_text(encoding="utf-8")).get("words") \
        if (session / "analysis.json").exists() else None
    if words is None and (gdir / "transcript.json").exists():
        words = json.loads((gdir / "transcript.json").read_text(encoding="utf-8"))
    if words is None:
        words = []
        if ev.get("has_audio") and meta["a_start"] is not None:
            progress("Preparing audio…")
            wav = session / "speech16k.wav"
            _extract_audio(raw, meta, wav, 16000, fill_gaps=ev.get("segments", 1) > 1)
            words = transcribe(wav, cfg, progress)
            wav.unlink(missing_ok=True)
        (gdir / "transcript.json").write_text(json.dumps(words), encoding="utf-8")
    transcript = "".join(w["word"] for w in words).strip()

    progress("Finding the steps…")
    steps = find_steps(ev, meta["duration"])
    intro_said = attach_narration(steps, words)
    progress("Taking screenshots…")
    screenshots(raw, meta, steps, gdir / "images", progress)

    progress("Writing the guide…")
    title = intro = ""
    tips = []
    try:
        title, intro, tips = interpret(cfg, intro_said, steps, gdir)
    except Exception:
        pass  # no Ollama: fall back to the narration itself
    for s in steps:
        s["text"] = s.get("text") or _fallback_text(s)
    if not title:
        page_list, app_list = describe.pages(ev, _Identity()), describe.apps(ev)
        title = f"How to: {page_list[0]['title'] if page_list else (app_list[0] if app_list else 'guide')}"
    intro = intro or intro_said

    when = datetime.strptime(session.name, "%Y-%m-%d_%H%M%S").strftime("%d %b %Y, %H:%M") \
        if re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}$", session.name) else session.name
    md = to_markdown(title, intro, steps, tips, describe.pages(ev, _Identity()), transcript, when,
                     cfg.guide_quotes is not False)
    path = gdir / "guide.md"
    path.write_text(md, encoding="utf-8")
    return {"guide": path, "steps": len([s for s in steps if not s.get("skip")])}
