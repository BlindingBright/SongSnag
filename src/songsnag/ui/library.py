"""The Library window: browse, search, retry and export the catalog."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QDesktopServices, QFont, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, browsers, youtube
from ..catalog import DONE, FAILED, QUEUED, SKIPPED, WORKING, Catalog
from ..config import AUDIO_FORMATS, AUDIO_FORMATS_SHORT, Settings
from .common import app_icon, open_path, reveal

STATUS_LABELS = {DONE: "Saved", QUEUED: "Waiting", WORKING: "Downloading…", SKIPPED: "Skipped", FAILED: "Failed"}
COLUMNS = ("Artist", "Title", "Album", "#", "Status", "Quality", "Plays", "Source", "Date", "Note")
SORT_KEY = Qt.ItemDataRole.UserRole + 2


class Item(QTableWidgetItem):
    """A cell that sorts by its SORT_KEY when it has one (dates, track numbers), else by its text."""

    def __lt__(self, other):
        a, b = self.data(SORT_KEY), other.data(SORT_KEY)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)
SOURCES = {
    **{b: f"Played in {app}" for b, app in browsers.APPS.items()},
    **{f"{b}-history": f"{app} history" for b, app in browsers.APPS.items()},
    "youtube-history": "YouTube history", "takeout": "Takeout", "album": "Album", "artist": "Artist",
    "playlist": "Playlist", "suggested": "Suggested", "followed": "New release",
}


class LibraryWindow(QWidget):
    def __init__(self, catalog: Catalog, settings: Settings, on_change, on_format, on_albums, on_get=None,
                 parent=None, activity=None):
        super().__init__(parent)
        self.catalog = catalog
        self.settings = settings
        self.on_change = on_change  # called after retry/forget so the worker wakes up
        self.on_format = on_format  # called with the new audio format key
        self.on_albums = on_albums  # called with video ids to look up album info for
        self.on_get = on_get  # called with a resolve spec (whole album / artist), and whether to follow
        self.activity = activity  # () -> the tray's one-line status ("Downloading: …")
        self.setWindowTitle(f"{APP_NAME} Library")
        self.setWindowIcon(app_icon())

        self.search = QLineEdit(placeholderText="Search artist, title, album…")
        self.search.setClearButtonEnabled(True)
        self.status = QComboBox()
        self.status.addItem("Everything", None)
        for key in (DONE, QUEUED, SKIPPED, FAILED):
            self.status.addItem(STATUS_LABELS[key], key)
        self.summary = QLabel()

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSortIndicator(COLUMNS.index("Date"), Qt.SortOrder.DescendingOrder)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(lambda _: self._play())

        retry = QPushButton("Download anyway")
        retry.setToolTip("Download the selected songs even if they were skipped or failed")
        retry.clicked.connect(self._retry)
        export_m3u = QPushButton("Export playlist…")
        export_m3u.clicked.connect(self._export_m3u)
        export_csv = QPushButton("Export CSV…")
        export_csv.clicked.connect(self._export_csv)
        self.format = QComboBox()
        self.format.setToolTip("File type for new downloads. Changing it offers to convert the songs you have.")
        for key in AUDIO_FORMATS_SHORT:
            self.format.addItem(AUDIO_FORMATS_SHORT[key], key)
            self.format.setItemData(self.format.count() - 1, AUDIO_FORMATS[key], Qt.ItemDataRole.ToolTipRole)
        self._sync_format()
        self.format.activated.connect(self._format_chosen)  # user picks only, not programmatic syncs

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.status)
        bottom = QHBoxLayout()
        bottom.addWidget(self.summary, 1)
        for b in (retry, export_m3u, export_csv):
            bottom.addWidget(b)
        bottom.addSpacing(12)
        bottom.addWidget(QLabel("Save songs as:"))
        bottom.addWidget(self.format)
        # One line while something downloads: what it is, and a busy bar. The queue itself is in the list.
        self.now = QLabel()
        bold = QFont(self.now.font())
        bold.setBold(True)
        self.now.setFont(bold)
        self.now.setTextFormat(Qt.TextFormat.PlainText)
        self.now.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # clip, never widen
        self.progress = QProgressBar(maximumHeight=8, textVisible=False)
        self.progress.setFixedWidth(140)
        self.progress.setRange(0, 0)  # busy indicator: yt-dlp doesn't report per-song progress to us
        self.strip = QWidget()
        strip = QHBoxLayout(self.strip)
        strip.setContentsMargins(4, 0, 4, 0)
        strip.addWidget(self.now, 1)
        strip.addWidget(self.progress)
        self.strip.hide()

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.strip)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)

        self._debounce = QTimer(self, singleShot=True, interval=250, timeout=self.refresh)
        self.search.textChanged.connect(self._debounce.start)
        self.status.currentIndexChanged.connect(self.refresh)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.search.setFocus)
        self._auto = QTimer(self, interval=5000, timeout=self._auto_refresh)
        self.resize(1000, 600)
        self._sized = False
        self._seen = None
        self._bold = QFont(self.table.font())
        self._bold.setBold(True)
        hl = self.palette().color(QPalette.ColorRole.Highlight)
        hl.setAlpha(60)
        self._highlight = QBrush(hl)
        self._dim = QBrush(self.palette().color(QPalette.ColorRole.PlaceholderText))

    def _sync_format(self) -> None:
        self.format.setCurrentIndex(max(0, self.format.findData(self.settings.audio_format)))

    def _format_chosen(self, _index: int) -> None:
        fmt = self.format.currentData()
        if fmt != self.settings.audio_format:
            self.on_format(fmt)
            self._sync_format()  # reflects the outcome (unchanged if the caller declined)
            self.refresh()

    def showEvent(self, e):
        self._sync_format()  # may have changed in Settings meanwhile
        self.refresh()
        self._auto.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self._auto.stop()
        super().hideEvent(e)

    def _auto_refresh(self):
        # Rebuilding thousands of rows is the Library's only real cost, so do it only when something changed.
        if self.catalog.version() != self._seen and not self.table.selectionModel().hasSelection():
            self.refresh()

    def update_activity(self) -> None:
        """The one-line "now downloading" strip, shown only while the worker is on a song."""
        current = self.activity() if self.activity else ""
        if not current.startswith(("Downloading", "Checking")):
            working = self.catalog.upcoming(1)
            if not working or working[0]["status"] != WORKING:
                self.strip.hide()
                return
            current = f"Downloading: {self._name(working[0])}"
        waiting = self.catalog.runnable_count()
        text = current + (f"   ·   {waiting} waiting" if waiting else "")
        self.now.setText(text)
        self.now.setToolTip(text)
        self.strip.show()

    @staticmethod
    def _name(r) -> str:
        if r["title"]:
            return f"{r['artist']} – {r['title']}" if r["artist"] else r["title"]
        return r["seen_title"] or r["video_id"]

    def refresh(self) -> None:
        self._seen = self.catalog.version()
        self.update_activity()
        rows = self.catalog.search(self.search.text().strip(), self.status.currentData())
        # Queue position, in the order the worker takes songs (live plays first, then oldest first).
        now = time.time()
        ready = sorted((r for r in rows if r["status"] == QUEUED and (r["next_try"] or 0) <= now),
                       key=lambda r: (-(r["priority"] or 0), r["first_seen"] or 0))
        position = {r["video_id"]: i + 1 for i, r in enumerate(ready)}
        date_col, track_col = COLUMNS.index("Date"), COLUMNS.index("#")
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            quality = ""
            if r["codec"]:
                quality = f"{r['codec']} {round(r['abr'])} kbps" if r["abr"] else r["codec"]
            stamp = r["downloaded_at"] or r["first_seen"] or 0
            status = STATUS_LABELS.get(r["status"], r["status"])
            # Newest first puts the song downloading now on top, then the queue in the order it'll download.
            order = stamp
            if r["status"] == WORKING:
                order = 4e12
            elif r["video_id"] in position:
                status, order = f"Waiting #{position[r['video_id']]}", 3e12 - position[r["video_id"]]
            elif r["status"] == QUEUED:
                retry = datetime.fromtimestamp(r["next_try"]).strftime("%H:%M")
                status, order = f"Retry at {retry}", 2e12 - (r["next_try"] or 0)
            values = (
                r["artist"] or "",
                r["title"] or r["seen_title"] or r["video_id"],
                r["album"] or "",
                r["track"] or "",
                status,
                quality,
                r["plays"],
                SOURCES.get(r["source"], r["source"] or ""),
                datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") if stamp else "",
                r["note"] or "",
            )
            working, waiting = r["status"] == WORKING, r["status"] == QUEUED
            for c, v in enumerate(values):
                item = Item()
                item.setData(Qt.ItemDataRole.DisplayRole, v)
                if c == date_col:
                    item.setData(SORT_KEY, order)
                elif c == track_col:  # sorting by # keeps each album together, in track order
                    item.setData(SORT_KEY, ((r["album"] or "").casefold(), r["track"] or 0))
                if working:  # the song being downloaded right now stands out
                    item.setFont(self._bold)
                    item.setBackground(self._highlight)
                elif waiting:
                    item.setForeground(self._dim)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, r["video_id"])
                    item.setData(Qt.ItemDataRole.UserRole + 1, r["path"])
                self.table.setItem(i, c, item)
        self.table.setSortingEnabled(True)
        if not self._sized and rows:
            self.table.resizeColumnsToContents()
            for c in (0, 1, 2):
                self.table.setColumnWidth(c, min(self.table.columnWidth(c), 240))
            self.table.setColumnWidth(track_col, 36)
            self._sized = True
        n = self.catalog.counts()
        self.summary.setText(f"{n[DONE]} saved · {n[QUEUED] + n[WORKING]} waiting · "
                             f"{n[SKIPPED]} skipped · {n[FAILED]} failed")

    def _selected(self) -> list[tuple[str, str | None]]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = []
        for r in rows:
            item = self.table.item(r, 0)
            out.append((item.data(Qt.ItemDataRole.UserRole), item.data(Qt.ItemDataRole.UserRole + 1)))
        return out

    def _menu(self, pos) -> None:
        sel = self._selected()
        if not sel:
            return
        m = QMenu(self)
        has_file = any(p and Path(p).exists() for _, p in sel)
        m.addAction("Play", self._play).setEnabled(has_file)
        m.addAction("Show in folder", self._reveal).setEnabled(has_file)
        m.addAction("Open on YouTube", self._open_youtube)
        m.addAction("Look up album info", self._albums).setEnabled(has_file)
        row = self.catalog.get(sel[0][0]) if len(sel) == 1 and self.on_get else None
        if row and row["artist"]:
            artist = youtube.primary_artist(row["artist"])
            m.addSeparator()
            if row["album"]:
                m.addAction(f"Get the whole album “{row['album']}”",
                            lambda: self.on_get(f"album:{artist}\t{row['album']}"))
            m.addAction(f"Get everything by {artist}", lambda: self.on_get(f"artist:{artist}"))
            m.addAction(f"Follow {artist} (new releases only)", lambda: self.on_get(f"follow:{artist}", True))
        m.addSeparator()
        m.addAction("Download anyway", self._retry)
        m.addAction("Remove from library", self._forget)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _play(self) -> None:
        for _, p in self._selected()[:1]:
            if p and Path(p).exists():
                open_path(Path(p))

    def _reveal(self) -> None:
        for _, p in self._selected()[:1]:
            if p and Path(p).exists():
                reveal(Path(p))

    def _open_youtube(self) -> None:
        for vid, _ in self._selected()[:5]:
            QDesktopServices.openUrl(QUrl(youtube.watch_url(vid)))

    def _albums(self) -> None:
        ids = [v for v, p in self._selected() if p and Path(p).exists()]
        if ids:
            self.on_albums(ids)

    def _retry(self) -> None:
        ids = [v for v, _ in self._selected()]
        if ids:
            self.catalog.requeue(ids, force=True)
            self.on_change()
            self.refresh()

    def _forget(self) -> None:
        ids = [v for v, _ in self._selected()]
        if ids and QMessageBox.question(
                self, "Remove from library",
                f"Forget {len(ids)} song(s)? Files already saved are kept on disk, and the songs will be "
                "downloaded again if you play them.") == QMessageBox.StandardButton.Yes:
            self.catalog.forget(ids)
            self.refresh()

    def _export_m3u(self) -> None:
        from ..config import load

        start = str(load().music_path / "SongSnag.m3u8")
        dest, _ = QFileDialog.getSaveFileName(self, "Export playlist", start, "Playlist (*.m3u8 *.m3u)")
        if dest:
            n = self.catalog.export_m3u(Path(dest))
            QMessageBox.information(self, "Playlist exported", f"Wrote {n} songs to {dest}")

    def _export_csv(self) -> None:
        start = str(Path.home() / f"songsnag-{time.strftime('%Y%m%d')}.csv")
        dest, _ = QFileDialog.getSaveFileName(self, "Export catalog", start, "CSV (*.csv)")
        if dest:
            n = self.catalog.export_csv(Path(dest))
            QMessageBox.information(self, "Catalog exported", f"Wrote {n} rows to {dest}")
