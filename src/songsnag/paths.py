"""Per-platform locations for settings, the catalog and the default music folder."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import APP_ID, APP_NAME


def _override() -> Path | None:
    # Lets tests and portable setups keep everything in one folder.
    root = os.environ.get("SONGSNAG_HOME")
    return Path(root) if root else None


def config_dir() -> Path:
    if root := _override():
        return root / "config"
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / APP_NAME
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / APP_ID


def data_dir() -> Path:
    if root := _override():
        return root / "data"
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_ID


def _xdg_music_dir() -> Path | None:
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^XDG_MUSIC_DIR="(.+)"', text, re.M)
    if not m:
        return None
    return Path(m.group(1).replace("$HOME", str(Path.home())))


def default_music_dir() -> Path:
    if root := _override():
        return root / "music"
    base = (_xdg_music_dir() if sys.platform.startswith("linux") else None) or Path.home() / "Music"
    return base / APP_NAME


def asset(name: str) -> Path:
    # Frozen builds unpack package data under sys._MEIPASS; source installs keep it beside this file.
    base = Path(getattr(sys, "_MEIPASS", "")) / "songsnag" if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / "assets" / name
