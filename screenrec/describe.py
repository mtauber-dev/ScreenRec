"""WhatsApp-ready text: title, steps, and links to every page shown in the video.

Uses a local Ollama model when one is running; otherwise builds the text from the transcript.
Links are always taken from what was actually on screen, never from the model.
"""
import json
import re
import shutil
import subprocess
import time
import urllib.request

BROWSER_SUFFIX = re.compile(r"(\s+and \d+ more pages?)?(\s[-—]\s[^-—]*)?\s[-—]\s"
                            r"(Google Chrome|Microsoft.?Edge|Mozilla Firefox|Brave)\s*$")
SKIP_URL = re.compile(r"^(https://)?(chrome|edge|about|brave)[:/]|newtab|localhost:\d+/?$|127\.0\.0\.1:\d+",
                      re.I)  # 127.0.0.1 = ScreenRec's own review window


def clean_title(title):
    return BROWSER_SUFFIX.sub("", title).strip()


def pages(ev, tm):
    """Distinct pages in the order they appear, with their output time."""
    seen, out = set(), []
    for w in ev.get("windows", []):
        url = w.get("url")
        if url and not SKIP_URL.search(url):
            key = url.split("?")[0].rstrip("/")
            if key not in seen:
                seen.add(key)
                out.append({"t": tm.out(w["t"]), "title": clean_title(w["title"]), "url": url})
    return out


def apps(ev):
    seen, out = set(), []
    for w in ev.get("windows", []):
        title = clean_title(w["title"])
        if title and title not in seen:
            seen.add(title)
            out.append(title)
    return out


def _mmss(s):
    return f"{int(s) // 60}:{int(s) % 60:02}"


def _ensure_ollama(cfg):
    """Start `ollama serve` in the background if it isn't running yet."""
    url = cfg.ollama_url.rstrip("/") + "/api/tags"
    try:
        urllib.request.urlopen(url, timeout=2)
        return
    except Exception:
        pass
    exe = shutil.which("ollama")
    if not exe:
        raise RuntimeError("ollama not installed")
    subprocess.Popen([exe, "serve"], creationflags=0x08000000, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            pass
    raise RuntimeError("ollama did not start")


def _ollama(cfg, transcript, page_list, app_list, actions):
    _ensure_ollama(cfg)
    prompt = f"""You turn screen recordings into short how-to messages for WhatsApp.

Transcript of the narrator:
\"\"\"{transcript or '(no narration)'}\"\"\"

Windows / pages shown, in order: {'; '.join(app_list) or 'unknown'}
Actions seen: {actions or 'none'}

Write ONLY:
1. A title line wrapped in single asterisks, e.g. *How to submit a report*
2. One short sentence saying what the video shows.
3. Numbered steps ("1. ", "2. " ...), at most 10, each under 15 words, starting with a verb.
   Put button or menu names in single asterisks.

Rules: WhatsApp formatting only (*bold*, _italic_). No markdown headings, no **double asterisks**,
no URLs, no closing remarks. Follow the narrator's wording where possible; do not invent steps."""
    body = json.dumps({"model": cfg.ollama_model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.2}}).encode()
    req = urllib.request.Request(cfg.ollama_url.rstrip("/") + "/api/generate", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        text = json.loads(r.read())["response"].strip()
    text = text.replace("**", "*")
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    return re.sub(r"https?://\S+", "", text).strip()


def _fallback(transcript, page_list, app_list):
    title = page_list[0]["title"] if page_list else (app_list[0] if app_list else "this")
    lines = [f"*How to: {title}*", ""]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", transcript) if len(s.split()) >= 3]
    if sentences:
        lines += [f"{i}. {s}" for i, s in enumerate(sentences[:10], 1)]
    else:
        lines += [f"{i}. Open *{a}*" for i, a in enumerate(app_list[:10], 1)]
    return "\n".join(lines)


def make_text(ev, words, tm, cfg):
    transcript = "".join(w["word"] for w in words).strip()
    page_list, app_list = pages(ev, tm), apps(ev)
    actions = ", ".join(sorted({k["label"] for k in ev.get("keys", [])}))
    body = None
    try:
        body = _ollama(cfg, transcript, page_list, app_list, actions)
    except Exception:
        pass
    body = body or _fallback(transcript, page_list, app_list)
    if page_list:
        body += "\n\n*Links*"
        for p in page_list:
            body += f"\n• {p['title']} ({_mmss(p['t'])}):\n{p['url']}"
    return body.strip() + "\n"


def to_clipboard(text):
    subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"], input=text,
                   text=True, encoding="utf-8", creationflags=0x08000000)


def file_to_clipboard(path):
    """Put the file itself on the clipboard, so Ctrl+V in WhatsApp attaches it."""
    subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -LiteralPath @($input)[0]"],
                   input=str(path), text=True, encoding="utf-8", creationflags=0x08000000)
