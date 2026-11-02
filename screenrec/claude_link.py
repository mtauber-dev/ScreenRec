"""Connecting Claude for writing documents: an Anthropic API key, or Claude Code if it's installed.

The API key is stored encrypted with Windows DPAPI, so only this Windows account on this PC can read it.
"""
import ctypes
import ctypes.wintypes as wt
import os
import shutil

from . import config

KEY_PATH = config.CONFIG_DIR / "anthropic.key"
MODEL = "claude-opus-5-5"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
        raise OSError("Windows could not encrypt/decrypt the key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def saved_key():
    try:
        return _dpapi(KEY_PATH.read_bytes(), protect=False).decode()
    except (OSError, ValueError):
        return None


def save_key(key):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_bytes(_dpapi(key.strip().encode(), protect=True))


def remove_key():
    KEY_PATH.unlink(missing_ok=True)


def claude_code():
    return shutil.which("claude.cmd") or shutil.which("claude.exe")


def api_client(key=None):
    """An Anthropic client from the given key, the saved key, or ANTHROPIC_API_KEY; else None."""
    import anthropic
    key = key or saved_key()
    if key:
        return anthropic.Anthropic(api_key=key)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.Anthropic()
    return None


def test_key(key):
    """Raises with a readable message when the key doesn't work. Listing models costs nothing."""
    import anthropic
    try:
        api_client(key).models.retrieve(MODEL)
    except anthropic.AuthenticationError:
        raise RuntimeError("That key was not accepted. Check it was copied in full.")
    except anthropic.PermissionDeniedError:
        raise RuntimeError("That key can't use Claude models. Check its workspace in the Anthropic Console.")
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't reach Anthropic. Check the internet connection.")


def status(cfg):
    key = saved_key()
    return {"writer": cfg.guide_writer or "claude",
            "api_key": f"{key[:7]}…{key[-4:]}" if key else None,
            "env_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "claude_code": bool(claude_code()),
            "connected": bool(key or os.environ.get("ANTHROPIC_API_KEY") or claude_code())}
