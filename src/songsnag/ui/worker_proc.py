"""Starting, watching and stopping the download worker process from the tray."""

from __future__ import annotations

import json
import logging
import sys
import time

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .. import APP_ID, paths
from ..catalog import Catalog
from ..config import Settings
from ..worker import COOLDOWN_KEY

log = logging.getLogger(__name__)


def worker_command(*extra: str) -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return sys.executable, list(extra)
    return sys.executable, ["-m", APP_ID, *extra]


class _Proc(QObject):
    """One worker process plus the JSON-lines events file it reports through."""

    line = Signal(dict)
    done = Signal(int, float)  # exit code, seconds it ran

    def __init__(self, name: str, args: list[str], parent: QObject):
        super().__init__(parent)
        self.events = paths.data_dir() / f"{name}-events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")
        self._offset = 0
        self._started = time.time()
        self.proc = QProcess(self)
        self.proc.setStandardOutputFile(QProcess.nullDevice())
        self.proc.setStandardErrorFile(QProcess.nullDevice())
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        self._tail = QTimer(self, interval=700, timeout=self._read)
        program, argv = worker_command(*args, "--events", str(self.events))
        self.proc.start(program, argv)
        self._tail.start()

    def running(self) -> bool:
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def _read(self) -> None:
        try:
            with self.events.open("r", encoding="utf-8") as f:
                f.seek(self._offset)
                chunk = f.read()
        except OSError:
            return
        complete = chunk[: chunk.rfind("\n") + 1]  # a line may still be half written
        self._offset += len(complete.encode("utf-8"))
        for raw in complete.splitlines():
            try:
                self.line.emit(json.loads(raw))
            except ValueError:
                log.warning("bad worker event: %r", raw)

    def _finished(self, code: int, status) -> None:
        self._tail.stop()
        self._read()
        crashed = status == QProcess.ExitStatus.CrashExit
        self.done.emit(-1 if crashed else code, time.time() - self._started)

    def _error(self, err) -> None:
        if err == QProcess.ProcessError.FailedToStart:
            log.error("could not start the download worker: %s", self.proc.errorString())
            self._tail.stop()
            self.done.emit(-1, 0.0)

    def stop(self) -> None:
        if not self.running():
            return
        if sys.platform == "win32":
            self.proc.kill()  # a windowless process ignores WM_CLOSE; its job is requeued next start
        else:
            self.proc.terminate()
        if not self.proc.waitForFinished(5000):
            self.proc.kill()
            self.proc.waitForFinished(2000)


class WorkerSupervisor(QObject):
    """Runs the download worker whenever the queue has something ready."""

    event = Signal(str, object)

    def __init__(self, catalog: Catalog, settings: Settings, parent: QObject | None = None):
        super().__init__(parent)
        self.catalog = catalog
        self.settings = settings
        self.current: str | None = None
        self._worker: _Proc | None = None
        self._importer: _Proc | None = None
        self._filler: _Proc | None = None
        self._fail_streak = 0
        self._backoff_until = 0.0
        self._stopping = False
        # Retries are scheduled in the catalog; look for due ones now and then.
        self._timer = QTimer(self, interval=30_000, timeout=self.kick)
        self._timer.start()

    def cooldown_until(self) -> float:
        return float(self.catalog.get_state(COOLDOWN_KEY, "0") or 0)

    def running(self) -> bool:
        return bool(self._worker and self._worker.running())

    def kick(self) -> None:
        """Start the worker if there is work and nothing is holding it back."""
        now = time.time()
        if (self._stopping or self.running() or self.settings.paused or now < self._backoff_until
                or now < self.cooldown_until() or not self.catalog.runnable_count()):
            return
        log.info("starting download worker")
        self._worker = _Proc("worker", ["--worker"], self)
        self._worker.line.connect(self._on_line)
        self._worker.done.connect(self._on_worker_done)

    def _on_line(self, msg: dict) -> None:
        kind, payload = msg.get("kind"), msg.get("payload")
        if kind == "busy":
            self.current = payload
        elif kind == "idle":
            self.current = None
        self.event.emit(kind, payload)

    def _on_worker_done(self, code: int, ran: float) -> None:
        self.current = None
        self.event.emit("idle", None)
        if code != 0 and ran < 20:
            # Crashing on start (broken install, bad yt-dlp update...): back off instead of spinning.
            self._fail_streak += 1
            delay = min(900, 15 * 2 ** self._fail_streak)
            self._backoff_until = time.time() + delay
            log.error("download worker exited with %s after %.0fs; retrying in %ds (see songsnag-worker.log)",
                      code, ran, delay)
            if self._fail_streak == 3:
                self.event.emit("error", "The downloader keeps failing to start. Details are in the log.")
        else:
            self._fail_streak = 0
        QTimer.singleShot(1000, self.kick)

    def import_account(self, limit: int) -> None:
        if self._importer and self._importer.running():
            return
        name = "YouTube account history"
        reported = []

        def on_line(m: dict) -> None:
            reported.append(m.get("kind"))
            self.event.emit(m.get("kind"), tuple(m.get("payload") or ()))

        def on_done(code: int, _ran: float) -> None:
            if not reported:  # crashed before it could say how it went
                self.event.emit("import_failed", (name, f"The importer stopped unexpectedly (exit {code}). "
                                                        "Details are in songsnag-worker.log."))
            self.kick()

        self._importer = _Proc("import", ["--worker", "--import-account", str(limit)], self)
        self._importer.line.connect(on_line)
        self._importer.done.connect(on_done)

    def fill_albums(self, video_ids: list[str] | None = None) -> bool:
        """Look up albums for saved songs (all not yet checked, or exactly these). False if already running."""
        if self._filler and self._filler.running():
            return False
        args = ["--worker", "--fill-albums"] + (["--ids", ",".join(video_ids)] if video_ids else [])
        self._filler = _Proc("albums", args, self)
        self._filler.line.connect(lambda m: self.event.emit(m.get("kind"), m.get("payload")))
        return True

    def stop(self) -> None:
        self._stopping = True
        self._timer.stop()
        for p in (self._worker, self._importer, self._filler):
            if p:
                p.stop()
