"""Start-at-login for Linux (XDG autostart), Windows (Run key) and macOS (LaunchAgent)."""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import sys
from pathlib import Path

from . import APP_ID, APP_NAME

log = logging.getLogger(__name__)


def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    script = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if script and script.name.startswith(APP_ID) and script.is_file():
        return [str(script.absolute())]  # the console script we were started from
    exe = shutil.which(APP_ID)
    if exe:
        return [exe]
    return [sys.executable, "-m", APP_ID]


def desktop_exec(cmd: list[str]) -> str:
    """Quote a command for a .desktop Exec= line (the spec's rules, not the shell's)."""
    out = []
    for arg in cmd:
        if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
            out.append(arg.replace("%", "%%"))
        else:
            escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
            out.append('"' + escaped.replace("%", "%%") + '"')
    return " ".join(out)


def _linux_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / f"{APP_ID}.desktop"


def _mac_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"io.github.{APP_ID}.plist"


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_enabled() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
            return True
        except OSError:
            return False
    return (_mac_file() if sys.platform == "darwin" else _linux_file()).exists()


def set_enabled(enabled: bool) -> None:
    try:
        _set(enabled)
    except Exception:
        log.exception("could not %s start-at-login", "enable" if enabled else "disable")


def _set(enabled: bool) -> None:
    cmd = launch_command()
    if sys.platform == "win32":
        import subprocess
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return

    target = _mac_file() if sys.platform == "darwin" else _linux_file()
    if not enabled:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        target.write_bytes(plistlib.dumps({
            "Label": f"io.github.{APP_ID}", "ProgramArguments": cmd, "RunAtLoad": True, "ProcessType": "Interactive",
        }))
    else:
        target.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "Comment=Save the music you play on YouTube in your browser\n"
            f"Exec={desktop_exec(cmd)}\n"
            f"Icon={APP_ID}\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-KDE-autostart-after=panel\n",
            encoding="utf-8",
        )
