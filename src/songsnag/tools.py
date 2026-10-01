"""Locating the external programs yt-dlp relies on. Must not import yt_dlp."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _bundle_dir() -> Path | None:
    # Frozen builds ship ffmpeg / deno in a "tools" folder inside the app's bundle directory
    # (PyInstaller's _internal), not next to the executable.
    if not getattr(sys, "frozen", False):
        return None
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tools"


def _which(name: str, extra: list[Path] = ()) -> str | None:
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in [p for p in (_bundle_dir(), *extra) if p]:
        cand = d / exe
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


def find_ffmpeg() -> str | None:
    return _which("ffmpeg")


def find_js_runtimes() -> dict:
    """yt-dlp needs a JavaScript runtime for YouTube. Enable every one we can find."""
    home = Path.home()
    extra = [Path(sys.executable).parent, home / ".deno" / "bin"]  # the deno PyPI wheel lands beside python
    found = {}
    for name, binary in (("deno", "deno"), ("node", "node"), ("bun", "bun"), ("quickjs", "qjs")):
        path = _which(binary, extra)
        if path:
            found[name] = {"path": path}
    return found
