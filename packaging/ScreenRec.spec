# PyInstaller spec: a one-folder build (dist\ScreenRec\ScreenRec.exe) with ffmpeg bundled.
# One folder, not one file: a single exe would unpack ~600 MB to a temp folder on every start.
# Build with packaging\build.ps1.
import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

HERE = Path(SPECPATH)
ROOT = HERE.parent
FFMPEG_DIR = Path(os.environ["SCREENREC_FFMPEG_DIR"])  # set by build.ps1

datas = [(str(ROOT / "screenrec" / "review.html"), "screenrec")]
datas += collect_data_files("faster_whisper")   # Silero VAD model
datas += collect_data_files("uiautomation")     # its UIAutomation helper DLLs
binaries = [(str(FFMPEG_DIR / "ffmpeg.exe"), "ffmpeg"), (str(FFMPEG_DIR / "ffprobe.exe"), "ffmpeg")]
binaries += collect_dynamic_libs("ctranslate2")

a = Analysis(
    [str(HERE / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # imported lazily inside functions, or picked per-platform at runtime
        "screenrec.tray", "screenrec.process", "screenrec.recorder", "screenrec.region", "screenrec.document",
        "pystray._win32", "pynput.keyboard._win32", "pynput.mouse._win32",
        "faster_whisper", "uiautomation", "screenrec.claude_link",
    ],
    excludes=["matplotlib", "pandas", "scipy", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ScreenRec",
    icon=str(HERE / "ScreenRec.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ScreenRec", upx=False)
