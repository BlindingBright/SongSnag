# PyInstaller spec for the Windows build (also works on macOS/Linux for testing).
#   pyinstaller packaging/songsnag.spec
# Put ffmpeg(.exe) and deno(.exe) in packaging/bin/ first to bundle them.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src" / "songsnag"
BIN = Path(SPECPATH) / "bin"
ICON = Path(SPECPATH) / "windows" / "songsnag.ico"

binaries = [(str(p), ".") for p in BIN.glob("*") if p.is_file()] if BIN.is_dir() else []

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=[(str(SRC / "assets"), "songsnag/assets"), *collect_data_files("ytmusicapi")],
    hiddenimports=["PySide6.QtSvg", "ytmusicapi", "mutagen"],
    excludes=["tkinter", "unittest", "pydoc", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
              "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.Qt3DCore"],
    # yt-dlp stays as plain .py files so a newer downloaded copy can shadow it at runtime.
    module_collection_mode={"yt_dlp": "py", "yt_dlp_ejs": "py"},
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="SongSnag",
    console=False,
    icon=str(ICON),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SongSnag", upx=False)
