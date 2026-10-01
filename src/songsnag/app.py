"""Entry point: the tray app, or (with --worker) the download worker it spawns."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

from . import APP_ID, APP_NAME, __version__, paths, ytdlp_update

log = logging.getLogger(__name__)


def setup_logging(debug: bool, name: str = APP_ID) -> Path:
    # The worker logs to its own file: two processes rotating one file breaks on Windows.
    logfile = paths.data_dir() / f"{name}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=2, encoding="utf-8")]
    if debug or (sys.stderr and sys.stderr.isatty()):
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return logfile


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_ID,
                                 description="Save the music you play on YouTube in Brave, Chrome or Edge.")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--debug", action="store_true", help="verbose logging to the terminal")
    ap.add_argument("--library", action="store_true", help="open the library window on start")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--events", help=argparse.SUPPRESS)
    ap.add_argument("--import-account", type=int, metavar="N", help=argparse.SUPPRESS)
    ap.add_argument("--fill-albums", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--ids", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.worker:
        setup_logging(args.debug, f"{APP_ID}-worker")
        ytdlp_update.activate()  # must happen before anything imports yt_dlp
        from . import worker

        return worker.main(args)

    logfile = setup_logging(args.debug)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .ui.common import InstanceGuard

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(False)

    guard = InstanceGuard()
    if guard.other_instance_running():
        log.info("already running; asked it to show itself")
        return 0

    log.info("%s %s starting (yt-dlp %s, Python %s)", APP_NAME, __version__, ytdlp_update.active_version(),
             sys.version.split()[0])
    from .ui.tray import Tray

    # Qt swallows SIGTERM/SIGINT; route them to a clean quit (logout, `kill`, Ctrl-C).
    def on_signal(*_):
        app.setProperty("quitting", True)  # tells startup code a closed dialog means "stop", not "OK"
        app.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    heartbeat = QTimer(interval=500)  # lets the Python interpreter run signal handlers
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start()

    tray = Tray(app, logfile)
    guard.activated.connect(tray.show_library)
    tray.start(show_library=args.library)
    if app.property("quitting"):
        tray.shutdown()
        return 0
    return app.exec()

