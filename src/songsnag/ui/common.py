"""Icons, file-manager helpers and the single-instance guard."""

from __future__ import annotations

import getpass
import hashlib
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .. import APP_ID, paths


@cache
def app_icon() -> QIcon:
    icon = QIcon()
    for name in ("songsnag.svg", "songsnag.png"):
        p = paths.asset(name)
        if p.exists():
            icon.addFile(str(p))
    return icon if not icon.isNull() else QIcon.fromTheme("audio-x-generic")


def _grey(pm: QPixmap) -> QPixmap:
    """The icon in solid grey, keeping its shape (alpha)."""
    img = pm.toImage()
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha():
                g = int(0.3 * c.red() + 0.59 * c.green() + 0.11 * c.blue()) // 2 + 70  # flat, mid grey
                img.setPixelColor(x, y, QColor(g, g, g, c.alpha()))
    return QPixmap.fromImage(img)


@cache
def tray_icon(state: str) -> QIcon:
    """idle: the logo as is. paused: grey with pause bars. busy / busy2: the two frames of the "working"
    flash (busy2 adds a green dot)."""
    base = app_icon().pixmap(64, 64)
    if state in ("idle", "busy"):
        return QIcon(base)
    pm = _grey(base) if state == "paused" else QPixmap(base)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    if state == "paused":
        p.setBrush(QColor("white"))
        p.drawRect(38, 38, 8, 22)
        p.drawRect(52, 38, 8, 22)
    elif state == "busy2":
        p.setBrush(QColor("#2ecc71"))
        p.setPen(QColor("white"))
        p.drawEllipse(QPointF(50, 50), 12, 12)
    p.end()
    return QIcon(pm)


def open_path(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def reveal(path: Path) -> None:
    """Show a file selected in the system file manager (falls back to opening its folder)."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
            return
        subprocess.Popen(["dbus-send", "--session", "--dest=org.freedesktop.FileManager1", "--type=method_call",
                          "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                          f"array:string:{QUrl.fromLocalFile(str(path)).toString()}", "string:"])
    except OSError:
        open_path(path.parent)


class InstanceGuard(QObject):
    """Only one SongSnag per user; a second launch asks the first to show its window."""

    activated = Signal()

    def __init__(self):
        super().__init__()
        self.name = f"{APP_ID}-{getpass.getuser()}"
        if os.environ.get("SONGSNAG_HOME"):  # a portable copy has its own catalog, so it's its own instance
            self.name += "-" + hashlib.sha1(str(paths.data_dir()).encode()).hexdigest()[:8]
        self.server: QLocalServer | None = None

    def other_instance_running(self) -> bool:
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if sock.waitForConnected(500):
            sock.write(b"show\n")
            sock.waitForBytesWritten(500)
            sock.disconnectFromServer()
            return True
        QLocalServer.removeServer(self.name)  # stale socket from a crash
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._on_connection)
        self.server.listen(self.name)
        return False

    def _on_connection(self) -> None:
        while self.server and self.server.hasPendingConnections():
            self.server.nextPendingConnection().deleteLater()
            self.activated.emit()
