"""The system tray icon, its menu, notifications, and wiring to the watcher and worker."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QSystemTrayIcon

from .. import APP_NAME, autostart, browsers, config, paths, ytdlp_update
from ..catalog import DONE, FAILED, QUEUED, SKIPPED, WORKING, Catalog
from ..engine import Engine
from ..tools import find_ffmpeg, find_js_runtimes
from .common import app_icon, open_path, reveal, tray_icon
from .dialogs import SettingsDialog, WelcomeDialog, pick_folder
from .library import LibraryWindow
from .worker_proc import WorkerSupervisor

log = logging.getLogger(__name__)
UPDATE_EVERY = 24 * 3600


class Tray(QObject):
    event = Signal(str, object)  # watcher/worker -> UI, crosses threads safely

    def __init__(self, app: QApplication, logfile: Path):
        super().__init__()
        self.app = app
        self.logfile = logfile
        self.settings = config.load()
        self.catalog = Catalog(paths.data_dir() / "catalog.db")
        self.engine = Engine(self.settings, self.catalog, self.event.emit)
        self.worker = WorkerSupervisor(self.catalog, self.settings, self)
        self.worker.event.connect(self._on_event)
        self.event.connect(self._on_event)
        self.library: LibraryWindow | None = None
        self._import_running: set[str] = set()
        self._batch_saved = 0

        self.icon = QSystemTrayIcon(tray_icon("idle"), self)
        self.icon.setToolTip(APP_NAME)
        self.menu = QMenu()
        self._build_menu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        app.aboutToQuit.connect(self.shutdown)

    # ---- startup ---------------------------------------------------------
    def start(self, show_library: bool = False) -> None:
        has_tray = QSystemTrayIcon.isSystemTrayAvailable()
        if has_tray:
            self.icon.show()
        if not self.settings.welcomed:
            self._welcome()
            if self.app.property("quitting"):
                return
        self._check_tools()
        self.engine.start()
        self.worker.kick()  # finish anything left in the queue from last time
        self._set_icon()
        if show_library or not has_tray:
            self.show_library()
            if not has_tray:
                box = QMessageBox(QMessageBox.Icon.Information, APP_NAME,
                                  "Your desktop has no system tray, so SongSnag is running in this window. "
                                  "Closing it keeps SongSnag running; start it again to reopen.",
                                  QMessageBox.StandardButton.Ok, self.library)
                box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                box.open()  # not exec(): a nested modal loop would block quitting
        self._update_timer = QTimer(self, interval=3600 * 1000, timeout=self._maybe_update_ytdlp)
        self._update_timer.start()
        QTimer.singleShot(60 * 1000, self._maybe_update_ytdlp)

    def _welcome(self) -> None:
        dlg = WelcomeDialog(self.settings)
        dlg.exec()
        if self.app.property("quitting"):
            return  # interrupted by logout/kill: ask again next time
        folder = dlg.folder.path()
        if folder:
            self.settings.music_dir = folder
        self.settings.autostart = dlg.autostart.isChecked()
        self.settings.welcomed = True
        config.save(self.settings)
        autostart.set_enabled(self.settings.autostart)
        # Arm the watcher first so the backfill and live cursor don't overlap confusingly.
        self.engine.poll_browsers()
        if dlg.backfill.isChecked():
            self._import("Browser history (30 days)", lambda: self.engine.import_browser_history(30))
        where = browsers.browsers_label(self.engine.profiles()) or "your browser"
        self._notify(f"{APP_NAME} is running",
                     f"Songs you play on YouTube in {where} will be saved to {self.settings.music_path}")

    def _check_tools(self) -> None:
        problems = []
        if not find_ffmpeg():
            problems.append("ffmpeg is not installed, so songs can't be tagged or given cover art.")
        if not find_js_runtimes():
            problems.append("No JavaScript runtime (Deno or Node.js) was found. YouTube needs one; "
                            "many downloads will fail until one is installed.")
        if not browsers.find_profiles():
            problems.append("No Brave, Chrome or Edge browser was found, so plays can't be detected automatically.")
        for p in problems:
            log.warning(p)
        if problems:
            self._notify(f"{APP_NAME} needs attention", " ".join(problems), warn=True)

    # ---- menu ------------------------------------------------------------
    def _build_menu(self) -> None:
        m = self.menu
        self.status_action = m.addAction(APP_NAME)
        self.status_action.setEnabled(False)
        self.counts_action = m.addAction("")
        self.counts_action.setEnabled(False)
        m.addSeparator()

        self.pause_action = QAction("Pause downloads", m, checkable=True)
        self.pause_action.setChecked(self.settings.paused)
        self.pause_action.toggled.connect(self._toggle_pause)
        m.addAction(self.pause_action)
        m.addAction("Open music folder", lambda: self._open_music_folder())
        m.addAction("Change music folder…", self.change_folder)
        self.recent_menu = m.addMenu("Recently saved")
        m.addAction("Library…", self.show_library)
        m.addAction("Fill in missing album info", lambda: self.fill_albums(None))
        m.addSeparator()

        imp = m.addMenu("Download from history")
        history_menu = imp.addMenu("Browser history")
        for label, days in (("Last 7 days", 7), ("Last 30 days", 30), ("Last year", 365), ("Everything", None)):
            a = history_menu.addAction(label)
            a.triggered.connect(lambda _=False, d=days, lb=label: self._import(
                f"Browser history ({lb.lower()})", lambda: self.engine.import_browser_history(d)))
        imp.addAction("My YouTube account history", self._import_account)
        imp.addAction("Google Takeout file…", self._import_takeout)
        self.retry_action = imp.addAction("Retry failed downloads", self._retry_failed)
        m.addSeparator()

        m.addAction("Settings…", self.show_settings)
        m.addAction("Open log", lambda: open_path(self.logfile))
        m.addAction(f"Quit {APP_NAME}", self.app.quit)
        m.aboutToShow.connect(self._refresh_menu)

    def _refresh_menu(self) -> None:
        self.status_action.setText(self._status_text())
        n = self.catalog.counts()
        self.counts_action.setText(f"{n[DONE]} saved · {n[QUEUED] + n[WORKING]} waiting · {n[SKIPPED]} skipped")
        self.retry_action.setEnabled(n[FAILED] > 0)
        self.retry_action.setText(f"Retry failed downloads ({n[FAILED]})")
        self.recent_menu.clear()
        recent = self.catalog.recent_downloads(10)
        for r in recent:
            path = Path(r["path"]) if r["path"] else None
            a = self.recent_menu.addAction(f"{r['artist']} – {r['title']}")
            a.triggered.connect(lambda _=False, p=path: p and p.exists() and reveal(p))
        if not recent:
            self.recent_menu.addAction("Nothing yet").setEnabled(False)

    def _status_text(self) -> str:
        if self.settings.paused:
            return "Paused"
        cooldown = self.worker.cooldown_until()
        if cooldown > time.time():
            mins = int((cooldown - time.time()) // 60) + 1
            return f"Downloads resume in {mins} min"
        if self.worker.current:
            return self.worker.current
        if self._import_running:
            return "Reading history…"
        if not self.settings.watch_enabled:
            return "Not watching your browser"
        where = browsers.browsers_label(self.engine.profiles())
        return f"Listening to {where}" if where else "No browser found"

    def _set_icon(self) -> None:
        state = "paused" if self.settings.paused else "busy" if self.worker.current else "idle"
        self.icon.setIcon(tray_icon(state))
        self.icon.setToolTip(f"{APP_NAME}: {self._status_text()}")

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_library()

    # ---- engine events ---------------------------------------------------
    def _on_event(self, kind: str, payload) -> None:
        if kind == "queued":
            self.worker.kick()
        elif kind in ("busy", "idle"):
            self._set_icon()
            if kind == "idle" and self._batch_saved and not self._import_running:
                self._notify("History import finished", f"Saved {self._batch_saved} songs.")
                self._batch_saved = 0
        elif kind == "downloaded":
            if payload["live"]:
                if self.settings.notify:
                    self._notify("Saved", f"{payload['artist']} – {payload['title']}")
            else:
                self._batch_saved += 1
        elif kind == "albums_started":
            self._notify("Album info", f"Looking up albums for {payload} songs…" if payload
                         else "Every song has already been checked.")
        elif kind == "albums_progress":
            done, total, found = payload
            self.icon.setToolTip(f"{APP_NAME}: album info {done}/{total} ({found} found)")
        elif kind == "albums_done":
            done, found = payload
            if done:
                self._notify("Album info", f"Added album info to {found} of {done} songs.")
            self._set_icon()
        elif kind in ("notice", "error"):
            self._notify(APP_NAME, str(payload), warn=True)
        elif kind == "import_started":
            self._import_running.add(payload)
            self._set_icon()
        elif kind == "import_done":
            name, n = payload
            self._import_running.discard(name)
            self._notify(f"{name}", f"Found {n} new videos to check." if n else "No new videos found.")
            self.worker.kick()
        elif kind == "import_failed":
            name, err = payload
            self._import_running.discard(name)
            QMessageBox.warning(None, f"{APP_NAME}: {name}", f"The import didn't work:\n\n{err}")

    def _notify(self, title: str, body: str, warn: bool = False) -> None:
        if self.icon.isVisible():
            icon = QSystemTrayIcon.MessageIcon.Warning if warn else QSystemTrayIcon.MessageIcon.Information
            self.icon.showMessage(title, body, app_icon() if not warn else icon, 8000)
        else:
            log.info("%s: %s", title, body)

    # ---- actions ---------------------------------------------------------
    def _toggle_pause(self, paused: bool) -> None:
        self.settings.paused = paused
        config.save(self.settings)  # a running worker sees this and stops after the current song
        if not paused:
            self.worker.kick()
        self._set_icon()

    def _open_music_folder(self) -> None:
        self.settings.music_path.mkdir(parents=True, exist_ok=True)
        open_path(self.settings.music_path)

    def change_folder(self) -> None:
        folder = pick_folder(None, str(self.settings.music_path))
        if folder:
            self._apply_folder(Path(folder))

    def _apply_folder(self, new: Path) -> None:
        old = self.settings.music_path
        if new.resolve() == old.resolve():
            return
        existing = self.catalog.done_under(old) if old.exists() else []
        self.settings.music_dir = str(new)
        config.save(self.settings)
        self.engine.settings_changed()
        question = f"Move the {len(existing)} songs already saved in\n{old}\nto\n{new}?"
        yes = QMessageBox.StandardButton.Yes
        if existing and QMessageBox.question(None, f"{APP_NAME}: move songs?", question) == yes:
            threading.Thread(target=self._move_songs, args=(existing, old, new), daemon=True).start()
        else:
            self._notify("Music folder changed", f"New songs will be saved to {new}")

    def _move_songs(self, rows, old: Path, new: Path) -> None:
        moved = 0
        for r in rows:
            src = Path(r["path"])
            if not src.exists():
                continue
            dst = new / src.resolve().relative_to(old.resolve())
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    shutil.move(src, dst)
                    moved += 1
                self.catalog.set_path(r["video_id"], str(dst))
            except OSError as e:
                log.warning("could not move %s: %s", src, e)
        for d in sorted((p for p in old.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()  # only removes folders we emptied
            except OSError:
                pass
        self.event.emit("notice", f"Moved {moved} songs to {new}")

    def show_settings(self) -> None:
        old_dir = self.settings.music_path
        old_format = self.settings.audio_format
        dlg = SettingsDialog(self.settings, autostart.is_enabled())
        if dlg.exec():
            new_dir = Path(dlg.folder.path()).expanduser() if dlg.folder.path() else old_dir
            dlg.apply()
            self.settings.music_dir = str(old_dir)  # _apply_folder handles the move prompt
            self.settings.autostart = dlg.autostart.isChecked()
            config.save(self.settings)
            autostart.set_enabled(self.settings.autostart)
            self.engine.settings_changed()
            self._apply_folder(new_dir)
            if self.settings.audio_format != old_format:
                self._offer_reformat()
            self._set_icon()

    def _login_app(self) -> str:
        p = browsers.login_profile(self.settings.login_profile, self.settings.profiles)
        return p.app if p else "your browser"

    def fill_albums(self, video_ids: list[str] | None) -> None:
        if not self.worker.fill_albums(video_ids):
            self._notify("Album info", "Already looking up albums; it'll finish in the background.")

    def set_format(self, fmt: str) -> None:
        """The Library's "Save songs as" dropdown."""
        self.settings.audio_format = fmt
        config.save(self.settings)  # the worker reads settings per song, so the next download uses it
        self._offer_reformat()

    def _offer_reformat(self) -> None:
        """After a format change, offer to redo saved songs so the whole library matches."""
        fmt = self.settings.audio_format
        if fmt == "best":
            return  # "original stream" is whatever each song already is
        rows = [r for r in self.catalog.search(status=DONE)
                if r["path"] and Path(r["path"]).suffix.lower() != f".{fmt}"]
        if not rows:
            return
        question = (f"{len(rows)} songs you already have aren't {fmt.upper()} yet.\n\n"
                    f"Download them again as {fmt.upper()}? The old files are replaced.")
        if QMessageBox.question(None, f"{APP_NAME}: convert library?", question) == QMessageBox.StandardButton.Yes:
            self.catalog.requeue([r["video_id"] for r in rows], force=True)
            self.worker.kick()

    def show_library(self) -> None:
        if self.library is None:
            self.library = LibraryWindow(self.catalog, self.settings, self.worker.kick, self.set_format,
                                         self.fill_albums)
        self.library.show()
        self.library.raise_()
        self.library.activateWindow()

    def _import(self, name: str, fn) -> None:
        if name in self._import_running:
            return
        self._notify(APP_NAME, f"Reading {name}… Music found there will download in the background.")
        self.engine.run_import(name, fn)

    def _import_account(self) -> None:
        if QMessageBox.question(
                None, f"{APP_NAME}: YouTube history",
                "SongSnag will read your YouTube watch history using the account you're signed into in "
                f"{self._login_app()} "
                "(up to the 1,000 most recent videos), then download the music among them.\n\nContinue?"
        ) == QMessageBox.StandardButton.Yes:
            name = "YouTube account history"
            if name not in self._import_running:
                self._import_running.add(name)
                self._notify(APP_NAME, f"Reading {name}… Music found there will download in the background.")
                self.worker.import_account(1000)

    def _import_takeout(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            None, "Choose your Takeout watch history", str(Path.home() / "Downloads"),
            "Watch history (watch-history.json watch-history.html *.json *.html)")
        if path:
            self._import("Takeout history", lambda: self.engine.import_takeout(Path(path)))

    def _retry_failed(self) -> None:
        n = self.catalog.retry_failed()
        self.worker.kick()
        self._notify(APP_NAME, f"Retrying {n} downloads.")

    # ---- yt-dlp updates --------------------------------------------------
    def _maybe_update_ytdlp(self) -> None:
        if not self.settings.auto_update_ytdlp:
            return
        last = float(self.catalog.get_state("ytdlp_checked", "0") or 0)
        if time.time() - last < UPDATE_EVERY:
            return
        self.catalog.set_state("ytdlp_checked", str(time.time()))

        def run():
            # The tray never loads yt-dlp, so the next download worker simply starts on the new version.
            try:
                ytdlp_update.check_and_install()
            except Exception as e:
                log.warning("yt-dlp update check failed: %s", e)

        threading.Thread(target=run, name="ytdlp-update", daemon=True).start()

    def shutdown(self) -> None:
        if getattr(self, "_down", False):
            return
        self._down = True
        self.icon.hide()
        self.worker.stop()
        self.engine.stop()
        self.catalog.close()

