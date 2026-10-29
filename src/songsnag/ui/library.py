"""The Library window: browse, search, retry and export the catalog."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QItemSelection, QItemSelectionModel, QModelIndex, Qt, QTimer, QUrl
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
    QSizePolicy,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, browsers, youtube
from ..catalog import DONE, FAILED, QUEUED, SKIPPED, WORKING, Catalog
from ..config import AUDIO_FORMATS, AUDIO_FORMATS_SHORT, Settings
from .common import app_icon, open_path, reveal

STATUS_LABELS = {DONE: "Saved", QUEUED: "Waiting", WORKING: "Downloading…", SKIPPED: "Skipped", FAILED: "Failed"}
COLUMNS = ("Artist", "Title", "Album", "#", "Status", "Quality", "Plays", "Source", "Date", "Note")
WIDTHS = (200, 260, 200, 40, 110, 110, 50, 130, 130)  # the last column stretches
DATE = COLUMNS.index("Date")
SLACK = 2.0  # seconds of overlap between incremental reloads, so a write landing mid-reload isn't missed
FULL_EVERY = 300  # seconds: a full reload this often while the window is open
SOURCES = {
    **{b: f"Played in {app}" for b, app in browsers.APPS.items()},
    **{f"{b}-history": f"{app} history" for b, app in browsers.APPS.items()},
    "youtube-history": "YouTube history", "takeout": "Takeout", "album": "Album", "artist": "Artist",
    "playlist": "Playlist", "suggested": "Suggested", "followed": "New release",
}


class Row:
    """One catalog row: its sort keys up front, its display text only once it's on screen."""

    __slots__ = ("raw", "video_id", "path", "status", "keys", "_text")

    def __init__(self, raw: tuple, position: dict[str, int]):
        (vid, status, _priority, next_try, first_seen, downloaded_at, artist, title, seen_title, album, track,
         codec, _abr, plays, source, note, path) = raw
        self.raw, self.video_id, self.path, self.status, self._text = raw, vid, path, status, None
        stamp = downloaded_at or first_seen or 0
        # Newest first puts the song downloading now on top, then the queue in the order it'll download.
        order = stamp
        if status == WORKING:
            order = 4e12
        elif vid in position:
            order = 3e12 - position[vid]
        elif status == QUEUED:
            order = 2e12 - (next_try or 0)
        title = title or seen_title or vid
        album_cf = (album or "").casefold()
        self.keys = ((artist or "").casefold(), title.casefold(), album_cf,
                     (album_cf, track or 0),  # by #: each album together, in track order
                     (status != WORKING, status, -order), codec or "", plays or 0, source or "", order,
                     (note or "").casefold())

    def text(self, position: dict[str, int]) -> tuple:
        if self._text is None:
            (vid, status, _p, next_try, first_seen, downloaded_at, artist, title, seen_title, album, track, codec,
             abr, plays, source, note, _path) = self.raw
            label = STATUS_LABELS.get(status, status)
            if vid in position:
                label = f"Waiting #{position[vid]}"
            elif status == QUEUED:
                label = f"Retry at {datetime.fromtimestamp(next_try or 0).strftime('%H:%M')}"
            quality = (f"{codec} {round(abr)} kbps" if abr else codec) if codec else ""
            stamp = downloaded_at or first_seen or 0
            self._text = (artist or "", title or seen_title or vid, album or "", str(track or ""), label, quality,
                          str(plays), SOURCES.get(source, source or ""),
                          datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") if stamp else "", note or "")
        return self._text


class LibraryModel(QAbstractTableModel):
    """The Library's rows. Qt asks only for the cells on screen, and sorting is one Python sort, so tens of
    thousands of songs stay quick (a QTableWidget makes an object per cell and calls back into Python for
    every comparison while sorting)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[Row] = []
        self.position: dict[str, int] = {}  # queue positions, for "Waiting #3"
        self.sort_column, self.sort_order = DATE, Qt.SortOrder.DescendingOrder
        self.bold = QFont()
        self.bold.setBold(True)
        self.highlight = self.dim = None

    def set_rows(self, rows: list[Row], position: dict[str, int] | None = None) -> None:
        self.beginResetModel()
        self.rows, self.position = rows, position or {}
        self._sort()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: B008 - Qt's signature
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.text(self.position)[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole and index.column() in (1, 2, 9):
            return row.text(self.position)[index.column()] or None
        if row.status == WORKING:  # the song being downloaded right now stands out
            if role == Qt.ItemDataRole.FontRole:
                return self.bold
            if role == Qt.ItemDataRole.BackgroundRole:
                return self.highlight
        elif row.status == QUEUED and role == Qt.ItemDataRole.ForegroundRole:
            return self.dim
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_column, self.sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        old = {r.video_id: i for i, r in enumerate(self.rows)}
        self._sort()
        # Keep selections and the current row on the same songs after reordering.
        moved = {old[r.video_id]: i for i, r in enumerate(self.rows)}
        persistent = self.persistentIndexList()
        self.changePersistentIndexList(persistent, [self.index(moved[i.row()], i.column()) for i in persistent])
        self.layoutChanged.emit()

    def _sort(self) -> None:
        reverse = self.sort_order == Qt.SortOrder.DescendingOrder
        c = self.sort_column
        if c != DATE:  # ties stay newest first (Python's sort is stable)
            self.rows.sort(key=lambda r: r.keys[DATE], reverse=True)
        self.rows.sort(key=lambda r: r.keys[c], reverse=reverse)


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

        self.model = LibraryModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().height() + 10)  # no measuring
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for c, w in enumerate(WIDTHS):
            header.resizeSection(c, w)
        header.setSortIndicator(DATE, Qt.SortOrder.DescendingOrder)
        self.table.setSortingEnabled(True)  # clicking a header calls LibraryModel.sort
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(lambda _: self._play())

        export = QToolButton(text="Export", popupMode=QToolButton.ToolButtonPopupMode.InstantPopup)
        export_menu = QMenu(export)
        export_menu.addAction("Playlist (M3U)…", self._export_m3u)
        export_menu.addAction("Spreadsheet (CSV)…", self._export_csv)
        export.setMenu(export_menu)
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
        # What the worker is on right now, next to the counts; the queue itself is in the list.
        self.now = QLabel()
        bold = QFont(self.now.font())
        bold.setBold(True)
        self.now.setFont(bold)
        self.now.setTextFormat(Qt.TextFormat.PlainText)
        self.now.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # clip, never widen
        bottom = QHBoxLayout()
        bottom.addWidget(self.summary)
        bottom.addSpacing(12)
        bottom.addWidget(self.now, 1)
        bottom.addWidget(export)
        bottom.addSpacing(12)
        bottom.addWidget(QLabel("Save songs as:"))
        bottom.addWidget(self.format)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)

        self._debounce = QTimer(self, singleShot=True, interval=250, timeout=lambda: self.refresh(full=True))
        self.search.textChanged.connect(self._debounce.start)
        self.status.currentIndexChanged.connect(lambda _: self.refresh(full=True))
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.search.setFocus)
        self._auto = QTimer(self, interval=5000, timeout=self._auto_refresh)
        self.resize(1000, 600)
        self._seen = self._since = None
        self._full_at = 0.0
        self.model.bold = QFont(self.table.font())
        self.model.bold.setBold(True)
        hl = self.palette().color(QPalette.ColorRole.Highlight)
        hl.setAlpha(60)
        self.model.highlight = QBrush(hl)
        self.model.dim = QBrush(self.palette().color(QPalette.ColorRole.PlaceholderText))

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
        self.model.set_rows([])  # a closed Library holds no rows: the tray stays small in the background
        self._seen = self._since = None
        super().hideEvent(e)

    def _auto_refresh(self):
        # Only when something changed, and then only the rows that did (a full reload now and then catches
        # anything a timestamp can't, like a song removed by another window).
        if self.catalog.version() != self._seen:
            self.refresh(full=time.time() - self._full_at > FULL_EVERY)

    def update_activity(self) -> None:
        """ "Checking: …" / "Downloading: …" in the bottom bar while the worker is on a song, else nothing."""
        current = self.activity() if self.activity else ""
        if not current.startswith(("Downloading", "Checking")):
            working = self.catalog.upcoming(1)
            current = (f"Downloading: {self._name(working[0])}"
                       if working and working[0]["status"] == WORKING else "")
        self.now.setText(current)
        self.now.setToolTip(current)

    @staticmethod
    def _name(r) -> str:
        if r["title"]:
            return f"{r['artist']} – {r['title']}" if r["artist"] else r["title"]
        return r["seen_title"] or r["video_id"]

    def refresh(self, full: bool = True) -> None:
        """Reload the list. full=False fetches only rows changed since the last reload and reuses the rest."""
        self._seen = self.catalog.version()
        self.update_activity()
        text, status = self.search.text().strip(), self.status.currentData()
        started = time.time()
        old = {r.video_id: r for r in self.model.rows}
        if full or self._since is None:
            raws = {r[0]: r for r in self.catalog.library_rows(text, status)}
            keep: dict[str, Row] = {}
            self._full_at = started
        else:
            since = self._since - SLACK
            fetched = self.catalog.library_rows(text, status, since=since)
            gone = self.catalog.changed_ids(since) - {r[0] for r in fetched}  # changed, no longer matches the filter
            changed = [r for r in fetched if r[0] not in old or old[r[0]].raw != r]  # overlap re-reads shown rows
            fresh = {r[0] for r in changed}
            if not changed and not (gone & old.keys()):
                self._since = started
                self._counts()
                return  # e.g. the tray wrote a setting: nothing in this view moved
            keep = {v: r for v, r in old.items() if v not in fresh and v not in gone}
            raws = {v: r.raw for v, r in keep.items()} | {r[0]: r for r in changed}
        self._since = started

        # Queue position, in the order the worker takes songs (live plays first, then oldest first).
        ready = sorted((r for r in raws.values() if r[1] == QUEUED and (r[3] or 0) <= started),  # status, next_try
                       key=lambda r: (-(r[2] or 0), r[4] or 0))  # priority, first_seen
        position = {r[0]: i + 1 for i, r in enumerate(ready)}
        was_queued = self.model.position
        rows = [keep[v] if v in keep and v not in position and v not in was_queued else Row(raw, position)
                for v, raw in raws.items()]

        # Reloading must not lose the user's place: remember the selection, current row and scroll position.
        chosen = {v for v, _ in self._selected()}
        current = self.table.currentIndex()
        current_id = self.model.rows[current.row()].video_id if current.isValid() else None
        scroll = self.table.verticalScrollBar().value()
        self.model.set_rows(rows, position)
        if chosen or current_id:
            where = {r.video_id: i for i, r in enumerate(self.model.rows)}
            sel = QItemSelection()
            for v in chosen:
                if v in where:
                    sel.select(self.model.index(where[v], 0), self.model.index(where[v], len(COLUMNS) - 1))
            sm = self.table.selectionModel()
            sm.select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            if current_id in where:
                sm.setCurrentIndex(self.model.index(where[current_id], current.column()),
                                   QItemSelectionModel.SelectionFlag.NoUpdate)
        self.table.verticalScrollBar().setValue(scroll)
        self._counts()

    def _counts(self) -> None:
        n = self.catalog.counts()
        self.summary.setText(f"{n[DONE]} saved · {n[QUEUED] + n[WORKING]} waiting · "
                             f"{n[SKIPPED]} skipped · {n[FAILED]} failed")

    def _selected(self) -> list[tuple[str, str | None]]:
        rows = self.model.rows
        picked = sorted(i.row() for i in self.table.selectionModel().selectedRows() if i.row() < len(rows))
        return [(rows[i].video_id, rows[i].path) for i in picked]

    def _selected_statuses(self) -> set[str]:
        rows = self.model.rows
        return {rows[i.row()].status for i in self.table.selectionModel().selectedRows() if i.row() < len(rows)}

    def _menu(self, pos) -> None:
        sel = self._selected()
        if not sel:
            return
        m = QMenu(self)
        has_file = any(p and Path(p).exists() for _, p in sel[:50])
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
        statuses = self._selected_statuses()
        if statuses & {SKIPPED, FAILED}:  # only where there's something to override
            label = "Retry download" if statuses & {SKIPPED, FAILED} == {FAILED} else "Download anyway"
            m.addAction(label, self._retry).setToolTip("Download even though it was skipped or failed")
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
