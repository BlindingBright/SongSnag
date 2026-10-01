"""Settings and first-run dialogs."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, browsers
from ..config import AUDIO_FORMATS, LAYOUTS, Settings
from .common import app_icon


def pick_folder(parent: QWidget | None, start: str) -> str | None:
    folder = QFileDialog.getExistingDirectory(parent, "Choose where to save your music", start)
    return folder or None


class FolderRow(QWidget):
    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.edit = QLineEdit(path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)

    def _browse(self) -> None:
        folder = pick_folder(self, self.edit.text())
        if folder:
            self.edit.setText(folder)

    def path(self) -> str:
        return self.edit.text().strip()


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, autostart_on: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} Settings")
        self.setWindowIcon(app_icon())
        self.settings = settings

        self.folder = FolderRow(str(settings.music_path))
        self.layout_box = QComboBox()
        for key, label in LAYOUTS.items():
            self.layout_box.addItem(label, key)
        self.layout_box.setCurrentIndex(list(LAYOUTS).index(settings.layout))
        self.format_box = QComboBox()
        for key, label in AUDIO_FORMATS.items():
            self.format_box.addItem(label, key)
        self.format_box.setCurrentIndex(list(AUDIO_FORMATS).index(settings.audio_format))
        self.cover = QCheckBox("Embed cover art")
        self.cover.setChecked(settings.embed_cover)
        self.square = QCheckBox("Crop cover art to a square")
        self.square.setChecked(settings.square_cover)
        self.cover.toggled.connect(self.square.setEnabled)
        self.square.setEnabled(settings.embed_cover)
        self.lookup_albums = QCheckBox("Look up album, year and track number on YouTube Music")
        self.lookup_albums.setChecked(settings.lookup_albums)
        self.album_art = QCheckBox("Use the official album cover when one is found")
        self.album_art.setChecked(settings.album_art)
        self.album_art.setEnabled(settings.lookup_albums and settings.embed_cover)
        for box in (self.lookup_albums, self.cover):
            box.toggled.connect(lambda _: self.album_art.setEnabled(
                self.lookup_albums.isChecked() and self.cover.isChecked()))

        files = QGroupBox("Files")
        f = QFormLayout(files)
        f.addRow("Music folder", self.folder)
        f.addRow("Organize as", self.layout_box)
        f.addRow("Audio format", self.format_box)
        f.addRow("", self.cover)
        f.addRow("", self.square)
        f.addRow("", self.lookup_albums)
        f.addRow("", self.album_art)

        self.watch = QCheckBox("Automatically download songs I play in my browser")
        self.watch.setChecked(settings.watch_enabled)
        self.music_only = QCheckBox("Only music (skip other YouTube videos)")
        self.music_only.setChecked(settings.music_only)
        self.max_len = QSpinBox()
        self.max_len.setRange(0, 600)
        self.max_len.setSuffix(" min")
        self.max_len.setSpecialValueText("No limit")
        self.max_len.setValue(settings.max_minutes)
        found = browsers.find_profiles()
        self.cookies = QCheckBox("Use my YouTube login from")
        self.cookies.setToolTip("Lets SongSnag get age-restricted songs and YouTube Premium's higher-bitrate audio.\n"
                                "Your login never leaves this computer.")
        self.cookies.setChecked(settings.use_cookies)
        self.login = QComboBox()
        for p in found:
            self.login.addItem(p.display, p.key)
        current = browsers.login_profile(settings.login_profile, settings.profiles)
        if current:
            self.login.setCurrentIndex(max(0, self.login.findData(current.key)))
        self.login.setEnabled(settings.use_cookies and bool(found))
        self.cookies.toggled.connect(lambda on: self.login.setEnabled(on and bool(found)))
        login_row = QWidget()
        lr = QHBoxLayout(login_row)
        lr.setContentsMargins(0, 0, 0, 0)
        lr.addWidget(self.cookies)
        lr.addWidget(self.login, 1)

        self.profiles = QListWidget()
        self.profiles.setMaximumHeight(90)
        chosen = set(settings.profiles)
        for p in found:
            item = QListWidgetItem(f"{p.display}  ({p.key})")
            item.setData(Qt.ItemDataRole.UserRole, p.key)
            item.setCheckState(Qt.CheckState.Checked if not chosen or p.key in chosen else Qt.CheckState.Unchecked)
            self.profiles.addItem(item)
        if not self.profiles.count():
            self.profiles.addItem("No Brave, Chrome or Edge profile was found on this computer")
            self.profiles.setEnabled(False)

        watching = QGroupBox("What to download")
        w = QFormLayout(watching)
        w.addRow(self.watch)
        w.addRow(self.music_only)
        w.addRow("Skip videos longer than", self.max_len)
        w.addRow(login_row)
        w.addRow("Watch these\nbrowser profiles", self.profiles)

        self.notify = QCheckBox("Show a notification for each new song")
        self.notify.setChecked(settings.notify)
        self.autostart = QCheckBox(f"Start {APP_NAME} when I log in")
        self.autostart.setChecked(autostart_on)
        self.update = QCheckBox("Keep the YouTube downloader (yt-dlp) up to date")
        self.update.setChecked(settings.auto_update_ytdlp)
        general = QGroupBox("General")
        g = QVBoxLayout(general)
        for cb in (self.notify, self.autostart, self.update):
            g.addWidget(cb)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        for box in (files, watching, general):
            lay.addWidget(box)
        lay.addWidget(buttons)
        self.resize(560, self.sizeHint().height())

    def apply(self) -> None:
        s = self.settings
        s.music_dir = str(Path(self.folder.path()).expanduser()) if self.folder.path() else s.music_dir
        s.layout = self.layout_box.currentData()
        s.audio_format = self.format_box.currentData()
        s.embed_cover = self.cover.isChecked()
        s.square_cover = self.square.isChecked()
        s.lookup_albums = self.lookup_albums.isChecked()
        s.album_art = self.album_art.isChecked()
        s.watch_enabled = self.watch.isChecked()
        s.music_only = self.music_only.isChecked()
        s.max_minutes = self.max_len.value()
        s.use_cookies = self.cookies.isChecked()
        if self.login.count():
            s.login_profile = self.login.currentData()
        s.notify = self.notify.isChecked()
        s.auto_update_ytdlp = self.update.isChecked()
        if self.profiles.isEnabled():
            items = [self.profiles.item(i) for i in range(self.profiles.count())]
            checked = [i.data(Qt.ItemDataRole.UserRole) for i in items if i.checkState() == Qt.CheckState.Checked]
            s.profiles = [] if len(checked) == len(items) else checked


class WelcomeDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Welcome to {APP_NAME}")
        self.setWindowIcon(app_icon())
        found = browsers.find_profiles()
        where = browsers.browsers_label(found) if found else "Brave, Chrome or Edge"

        intro = QLabel(
            f"<h3>{APP_NAME} saves the music you play on YouTube in {where}.</h3>"
            "<p>It runs quietly in your system tray. Whenever you play a song on YouTube or YouTube Music, "
            "it downloads the best-quality audio, tags it and files it in your music folder.</p>"
            + ("" if found else "<p><b>No Brave, Chrome or Edge browser was found on this computer.</b> You can "
                                "still import a Google Takeout watch history from the tray menu.</p>"))
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        self.folder = FolderRow(str(settings.music_path))
        self.backfill = QCheckBox("Also download music from my browser history (last 30 days)")
        self.backfill.setChecked(bool(found))
        self.backfill.setEnabled(bool(found))
        self.autostart = QCheckBox(f"Start {APP_NAME} when I log in")
        self.autostart.setChecked(True)

        form = QFormLayout()
        form.addRow("Save music to", self.folder)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start")
        buttons.accepted.connect(self.accept)

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(form)
        lay.addWidget(self.backfill)
        lay.addWidget(self.autostart)
        lay.addWidget(buttons)
        self.resize(540, self.sizeHint().height())
