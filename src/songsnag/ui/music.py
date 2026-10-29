"""Getting more music: whole albums and artists, followed artists, and suggestions."""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, youtube
from ..catalog import Catalog
from .common import app_icon


def spec_for(kind: str, text: str, artist: str = "") -> str:
    """What the worker's --resolve understands."""
    text = text.strip()
    if kind == "link":
        return text
    if kind == "album" and artist:
        return f"album:{artist}\t{text}"
    return f"{kind}:{text}"


class GetMusicDialog(QDialog):
    """Ask for an artist, an album or a link; the tray resolves it and confirms before queueing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: get an album or artist")
        self.setWindowIcon(app_icon())
        intro = QLabel("Download a whole album, everything by an artist, or a playlist. Songs come from "
                       "YouTube Music with full album details, and anything you already have is skipped.")
        intro.setWordWrap(True)
        self.kinds = QButtonGroup(self)
        row = QHBoxLayout()
        for i, (key, label) in enumerate((("artist", "Artist"), ("album", "Album"), ("link", "Link"))):
            b = QRadioButton(label)
            b.setProperty("kind", key)
            b.setChecked(i == 0)
            self.kinds.addButton(b)
            row.addWidget(b)
        row.addStretch(1)
        self.text = QLineEdit()
        self.artist = QLineEdit(placeholderText="Artist (optional, makes the album search exact)")
        self.follow = QCheckBox("Also download new releases from this artist automatically")
        self.kinds.buttonToggled.connect(lambda *_: self._sync())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Find")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(row)
        lay.addWidget(self.text)
        lay.addWidget(self.artist)
        lay.addWidget(self.follow)
        lay.addWidget(buttons)
        self._sync()
        self.resize(480, self.sizeHint().height())

    def kind(self) -> str:
        return self.kinds.checkedButton().property("kind")

    def _sync(self) -> None:
        kind = self.kind()
        self.text.setPlaceholderText({"artist": "Artist name, e.g. Little Dragon",
                                      "album": "Album title",
                                      "link": "music.youtube.com/… or youtube.com/playlist?list=…"}[kind])
        self.artist.setVisible(kind == "album")
        self.follow.setVisible(kind == "artist")

    def spec(self) -> str:
        return spec_for(self.kind(), self.text.text(), self.artist.text().strip())


def confirm_collection(parent, c: dict, follow_checked: bool) -> tuple[bool, bool]:
    """(queue the songs?, follow the artist?) for a resolved album/artist/playlist."""
    n = len(c["tracks"])
    kind = c["kind"]
    if kind == "artist" and not c["tracks"]:  # "follow only" requests
        return False, True
    what = {"artist": f"everything by {c['name']}", "album": c["name"], "playlist": f"the playlist “{c['name']}”"}
    box = QMessageBox(QMessageBox.Icon.Question, f"{APP_NAME}: download {kind}?",
                      f"Download {what.get(kind, c['name'])}?\n\n{n} songs. Songs you already have are skipped, "
                      "and they download in the background a few seconds apart.", parent=parent)
    follow = None
    if kind == "artist":
        follow = QCheckBox("Also get new releases automatically")
        follow.setChecked(follow_checked)
        box.setCheckBox(follow)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    box.button(QMessageBox.StandardButton.Yes).setText(f"Download {n} songs")
    if kind == "artist":
        only = box.addButton("Only new releases", QMessageBox.ButtonRole.AcceptRole)
    box.exec()
    clicked = box.clickedButton()
    if kind == "artist" and clicked is only:
        return False, True
    yes = clicked is box.button(QMessageBox.StandardButton.Yes)
    return yes, bool(yes and follow and follow.isChecked())


class ArtistSearchWindow(QWidget):
    """Find an artist on YouTube Music, including ones not in the library yet, and get or follow them."""

    def __init__(self, on_search, on_pick, parent=None):
        super().__init__(parent)
        self.on_search = on_search  # (name) -> asks the tray to search in a worker
        self.on_pick = on_pick  # (artist id, name, follow) -> the usual look-up-and-confirm flow
        self.setWindowTitle(f"{APP_NAME}: find an artist")
        self.setWindowIcon(app_icon())
        self.query = QLineEdit(placeholderText="Artist name")
        self.query.returnPressed.connect(self.search)
        self.go = QPushButton("Search")
        self.go.setDefault(True)
        self.go.clicked.connect(self.search)
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(lambda _: self._pick(False))
        self.results.currentItemChanged.connect(lambda *_: self._sync())
        self.info = QLabel("Search YouTube Music for any artist, including ones you haven't played yet.")
        self.info.setWordWrap(True)
        self.get = QPushButton("Download their music…")
        self.get.clicked.connect(lambda: self._pick(False))
        self.follow = QPushButton("Follow (new releases only)")
        self.follow.clicked.connect(lambda: self._pick(True))
        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        row.addWidget(self.go)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.follow)
        buttons.addWidget(self.get)
        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(self.info)
        lay.addWidget(self.results, 1)
        lay.addLayout(buttons)
        self.resize(460, 380)
        self._sync()

    def open_with(self, text: str) -> None:
        if text:
            self.query.setText(text)
        self.show()
        self.raise_()
        self.activateWindow()
        self.query.setFocus()
        if text:
            self.search()

    def search(self) -> None:
        name = self.query.text().strip()
        if not name:
            return
        self.results.clear()
        self.go.setEnabled(False)
        self.info.setText(f"Searching YouTube Music for “{name}”…")
        self.on_search(name)
        self._sync()

    def show_results(self, query: str, found: list[dict]) -> None:
        self.go.setEnabled(True)
        if query != self.query.text().strip():
            return  # an older search finishing after a newer one started
        self.results.clear()
        for a in found:
            have = f"  —  {a['saved']} songs in your library" if a.get("saved") else "  —  new to your library"
            item = QListWidgetItem(a["name"] + have)
            item.setData(Qt.ItemDataRole.UserRole, (a["id"], a["name"]))
            self.results.addItem(item)
        if found:
            self.results.setCurrentRow(0)
            self.info.setText("Pick the right one (several artists can share a name), then download or follow.")
        else:
            self.info.setText(f"No artist called “{query}” on YouTube Music.")
        self._sync()

    def search_failed(self, err: str) -> None:
        self.go.setEnabled(True)
        self.info.setText(f"Search didn't work: {err}")

    def _sync(self) -> None:
        picked = self.results.currentItem() is not None
        self.get.setEnabled(picked)
        self.follow.setEnabled(picked)

    def _pick(self, follow_only: bool) -> None:
        item = self.results.currentItem()
        if item:
            artist_id, name = item.data(Qt.ItemDataRole.UserRole)
            self.on_pick(artist_id, name, follow_only)


class DiscoverWindow(QWidget):
    COLUMNS = ("Artist", "Title", "Album", "Why")

    def __init__(self, catalog: Catalog, on_refresh, on_queued, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.on_refresh = on_refresh  # asks the tray to compute new suggestions
        self.on_queued = on_queued  # wakes the download worker
        self.setWindowTitle(f"{APP_NAME}: discover")
        self.setWindowIcon(app_icon())
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(lambda _: self._listen())

        self.refresh_btn = QPushButton("Find new suggestions")
        self.refresh_btn.clicked.connect(self._refresh)
        listen = QPushButton("Listen on YouTube")
        listen.clicked.connect(self._listen)
        get = QPushButton("Download selected")
        get.clicked.connect(lambda: self._answer("queued", self._selected()))
        get_all = QPushButton("Download all")
        get_all.clicked.connect(lambda: self._answer("queued", self._all()))
        no = QPushButton("Not interested")
        no.setToolTip("Never suggest these again")
        no.clicked.connect(lambda: self._answer("dismissed", self._selected()))

        bottom = QHBoxLayout()
        bottom.addWidget(self.refresh_btn)
        bottom.addStretch(1)
        for b in (listen, no, get, get_all):
            bottom.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)
        self.resize(900, 520)
        self.busy = False

    def showEvent(self, e):
        self.reload()
        super().showEvent(e)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.refresh_btn.setEnabled(not busy)
        self.reload()

    def reload(self) -> None:
        rows = self.catalog.suggestions()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for c, v in enumerate((r["artist"], r["title"], r["album"] or "", r["reason"] or "")):
                item = QTableWidgetItem(v or "")
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, r["video_id"])
                self.table.setItem(i, c, item)
        if rows:
            self.table.resizeColumnsToContents()
            for c in (0, 1, 2):
                self.table.setColumnWidth(c, min(self.table.columnWidth(c), 260))
        when = float(self.catalog.get_state("discovered_at", "0") or 0)
        if self.busy:
            self.info.setText("Looking for music you might like, based on the songs you play most…")
        elif not when:
            self.info.setText("Suggestions come from YouTube Music radio for the songs you play most. A song is "
                              "only suggested when several of your favourites lead to it, or it's by an artist "
                              "you already play. Click “Find new suggestions” to start.")
        else:
            stamp = datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
            self.info.setText(f"{len(rows)} suggestions, found {stamp}. Double-click one to listen first."
                              if rows else f"Nothing new to suggest right now (checked {stamp}).")

    def _ids(self, rows) -> list[str]:
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]

    def _selected(self) -> list[str]:
        return self._ids(sorted({i.row() for i in self.table.selectedIndexes()}))

    def _all(self) -> list[str]:
        return self._ids(range(self.table.rowCount()))

    def _refresh(self) -> None:
        self.on_refresh()

    def _listen(self) -> None:
        ids = self._selected()[:3]
        self.catalog.mark_previewed(ids)  # the browser play must not download it; "Download" still can
        for vid in ids:
            QDesktopServices.openUrl(QUrl(youtube.watch_url(vid, music=True)))

    def _answer(self, state: str, ids: list[str]) -> None:
        if not ids:
            return
        self.catalog.answer_suggestions(ids, state)
        if state == "queued":
            self.on_queued()
        self.reload()


class FollowsDialog(QDialog):
    """Artists whose new releases download automatically."""

    def __init__(self, catalog: Catalog, on_check, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle(f"{APP_NAME}: followed artists")
        self.setWindowIcon(app_icon())
        intro = QLabel("New albums and singles by these artists are downloaded automatically (checked once a "
                       "day). Follow an artist from “Get an album or artist…” or the Library.")
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        remove = QPushButton("Unfollow")
        remove.clicked.connect(self._unfollow)
        check = QPushButton("Check now")
        check.clicked.connect(lambda: (on_check(), self.accept()))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(remove)
        row.addWidget(check)
        row.addStretch(1)
        row.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        self._fill()
        self.resize(420, 360)

    def _fill(self) -> None:
        self.list.clear()
        for f in self.catalog.follows():
            when = (f"checked {time.strftime('%Y-%m-%d', time.localtime(f['last_checked']))}"
                    if f["last_checked"] else "not checked yet")
            item = QListWidgetItem(f"{f['name']}  ({when})")
            item.setData(Qt.ItemDataRole.UserRole, f["artist_id"])
            self.list.addItem(item)
        if not self.list.count():
            self.list.addItem("You're not following anyone yet.")
            self.list.setEnabled(False)

    def _unfollow(self) -> None:
        ids = [i.data(Qt.ItemDataRole.UserRole) for i in self.list.selectedItems() if i.data(Qt.ItemDataRole.UserRole)]
        if ids:
            self.catalog.unfollow(ids)
            self._fill()
