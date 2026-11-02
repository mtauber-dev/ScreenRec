"""Claude for writing documents goes through Claude Code, when it's installed and signed in."""
import shutil


def claude_code():
    return shutil.which("claude.cmd") or shutil.which("claude.exe")


def status(cfg):
    return {"writer": cfg.guide_writer or "claude", "claude_code": bool(claude_code())}
