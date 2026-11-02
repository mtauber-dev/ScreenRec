"""Small Win32 helpers: DPI, monitors, foreground window, browser URL, ffmpeg path."""
import ctypes
import ctypes.wintypes as wt
import re
import shutil
import subprocess
import sys
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
NO_WINDOW = 0x08000000

BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}


def set_dpi_aware():
    # Physical pixels everywhere, so ffmpeg, pynput and tkinter agree on coordinates.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def monitors():
    result = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(_h, _dc, rect, _lp):
        r = rect.contents
        result.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    return result


def cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def monitor_at(x, y):
    mons = monitors()
    for m in mons:
        if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]:
            return m
    return mons[0]


def virtual_screen():
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))


def foreground():
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        path = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
            exe = Path(path.value).name.lower()
        kernel32.CloseHandle(h)
    return hwnd, buf.value, exe


def browser_url(hwnd):
    """Read the address bar via UI Automation. Caller's thread must have UIA initialised."""
    import uiautomation as auto
    try:
        win = auto.ControlFromHandle(hwnd)
        edit = win.EditControl(searchDepth=16, AutomationId="urlbar-input")  # Firefox
        if not edit.Exists(0, 0):
            edit = win.EditControl(searchDepth=16, RegexName=r"(?i)address")  # Chrome / Edge
        if not edit.Exists(0, 0):
            return None
        value = edit.GetValuePattern().Value.strip()
    except Exception:
        return None
    if not value or " " in value or ("." not in value and "://" not in value):
        return None
    if "://" not in value:
        value = "https://" + value
    return value.split("#")[0]


UIA_KINDS = {"Button": "button", "Hyperlink": "link", "MenuItem": "menu item", "TabItem": "tab",
             "Edit": "field", "ListItem": "list item", "CheckBox": "checkbox", "RadioButton": "option",
             "ComboBox": "dropdown", "TreeItem": "item", "DataItem": "item", "Text": "text",
             "SplitButton": "button", "Image": "icon", "MenuBar": "menu"}
UIA_STOP = {"Pane", "Document", "Window", "TitleBar"}  # reached the page or window itself


def element_at(x, y):
    """Name and kind of what's at a screen point, e.g. {"name": "Manage", "kind": "button"}, or None.
    Caller's thread must have UIA initialised."""
    import uiautomation as auto
    try:
        c = auto.ControlFromPoint(x, y)
        for _ in range(4):  # an unnamed icon or label inside a named button: use the button
            if c is None:
                return None
            kind = c.ControlTypeName.replace("Control", "")
            name = " ".join((c.Name or "").split())
            if kind in UIA_STOP:
                return None
            if name and len(name) <= 80:
                return {"name": name, "kind": UIA_KINDS.get(kind, kind.lower())}
            c = c.GetParentControl()
    except Exception:
        pass
    return None


def ffmpeg_exe(name="ffmpeg"):
    """The packaged exe's own ffmpeg first; else PATH, resolving scoop shims to the real exe so
    stdin/terminate reach ffmpeg itself."""
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "ffmpeg" / f"{name}.exe"
        if bundled.exists():
            return str(bundled)
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} not found on PATH")
    shim = Path(path).with_suffix(".shim")
    if shim.exists():
        m = re.search(r'path\s*=\s*"?([^"\r\n]+)', shim.read_text())
        if m and Path(m.group(1)).exists():
            return m.group(1)
    return path


def media_duration(path):
    """Container duration in seconds, or None if ffprobe can't read it."""
    try:
        out = subprocess.run([ffmpeg_exe("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
        return float(out.strip())
    except (ValueError, OSError):
        return None


def exclude_from_capture(hwnd):
    """Hide a window from screen capture (Windows 10 2004+), so overlays never end up in the video."""
    return bool(user32.SetWindowDisplayAffinity(hwnd, 0x11))  # WDA_EXCLUDEFROMCAPTURE


def make_overlay(hwnd, click_through=False):
    """Never take focus from the app being recorded; optionally let clicks pass straight through."""
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = -20, 0x80, 0x08000000
    WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if click_through:
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def list_mics():
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         creationflags=NO_WINDOW).stderr
    return re.findall(r'"([^"]+)" \(audio\)', out)
