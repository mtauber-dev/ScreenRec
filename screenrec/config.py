"""Settings, stored in config.json. Edit that file to tweak behaviour.

From source it sits next to the project; the packaged exe keeps it in %APPDATA%\\ScreenRec, since
an installed program can't write next to itself."""
import json
import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ScreenRec" if FROZEN else ROOT
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "output_dir": str(Path.home() / "Videos" / "ScreenRec"),
    "mic": None,                         # dshow device name; null = no audio (first run picks the first mic)
    "hotkey": "<ctrl>+<alt>+r",          # start (full screen under cursor) / stop
    "capture_fps": 30,
    "capture_encoder": "h264_qsv",       # Intel QuickSync; falls back to libx264
    "out_fps": 15,
    "out_height": 720,
    "target_mb": 15,                     # final mp4 stays under this
    "trim_idle": True,
    "min_idle_gap": 1.5,                 # idle stretches longer than this (s) get cut
    "idle_pad": 0.5,                     # seconds kept either side of activity
    "zoom_enabled": True,
    "zoom": 1.5,
    "click_highlights": True,
    "key_overlay": True,                 # shows shortcuts / Enter / Esc only, never typed text
    "captions": True,
    "whisper_model": "small",
    "language": "en",                    # null = auto-detect
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen2.5:3b",
    "keep_raw": True,
    "output": "video",                   # after recording, make: "video", "document" or "both"
    "guide_quotes": True,                # document: show what was said under each step
    "guide_writer": "claude",            # document: "claude" (Claude Code; sends transcript + screenshots
                                         # to Anthropic) or "ollama" (stays on this PC)
    # "Send to WhatsApp" in the review window (opens Claude Code with the mcp-whatsapp plugin, where you
    # approve the send). null = copy by hand
    "whatsapp_chat_id": None,            # e.g. "1203630000000000@g.us"
    "whatsapp_chat_name": "",            # that chat's title, e.g. "Tutorials"
}


class Config(dict):
    __getattr__ = dict.get


def load():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))  # Notepad may add a BOM
    else:
        from . import winutil
        try:
            cfg["mic"] = next(iter(winutil.list_mics()), None)
        except Exception:
            pass
        save(cfg)
    return Config(cfg)


def save(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(dict(cfg), indent=2), encoding="utf-8")
