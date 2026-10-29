"""Finding browser profiles and reading YouTube plays out of their history.

Brave, Chrome, Edge, Vivaldi, Opera and Chromium share Chromium's History
database; Firefox keeps its own in places.sqlite. Browsers keep these locked
while running, so every read works on a private copy. The copy includes the
rollback journal or write-ahead log, letting SQLite discard a half-written
transaction instead of handing us a torn page.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import youtube

log = logging.getLogger(__name__)

WEBKIT_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01
APPS = {"brave": "Brave", "chrome": "Chrome", "edge": "Edge", "vivaldi": "Vivaldi", "opera": "Opera",
        "chromium": "Chromium", "firefox": "Firefox"}
SUPPORTED = "Brave, Chrome, Edge, Firefox, Vivaldi, Opera or Chromium"


@dataclass(frozen=True)
class Root:
    """One browser's "User Data" folder."""

    browser: str  # yt-dlp's name for it: brave / chrome / edge / vivaldi / opera / chromium / firefox
    label: str  # stable prefix for profile keys; stored in settings and catalog, so never change these
    path: Path

    @property
    def firefox(self) -> bool:
        return self.browser == "firefox"


def _candidates() -> list[Root]:
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        table = [
            ("brave", "Brave-Browser", local / "BraveSoftware/Brave-Browser/User Data"),
            ("brave", "Brave-Browser-Beta", local / "BraveSoftware/Brave-Browser-Beta/User Data"),
            ("brave", "Brave-Browser-Nightly", local / "BraveSoftware/Brave-Browser-Nightly/User Data"),
            ("chrome", "Chrome", local / "Google/Chrome/User Data"),
            ("chrome", "Chrome-Beta", local / "Google/Chrome Beta/User Data"),
            ("chrome", "Chrome-Dev", local / "Google/Chrome Dev/User Data"),
            ("chrome", "Chrome-Canary", local / "Google/Chrome SxS/User Data"),
            ("edge", "Edge", local / "Microsoft/Edge/User Data"),
            ("edge", "Edge-Beta", local / "Microsoft/Edge Beta/User Data"),
            ("edge", "Edge-Dev", local / "Microsoft/Edge Dev/User Data"),
            ("vivaldi", "Vivaldi", local / "Vivaldi/User Data"),
            ("chromium", "Chromium", local / "Chromium/User Data"),
            ("opera", "Opera", roaming / "Opera Software/Opera Stable"),
            ("opera", "Opera-GX", roaming / "Opera Software/Opera GX Stable"),
            ("firefox", "Firefox", roaming / "Mozilla/Firefox"),
        ]
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        table = [
            ("brave", "Brave-Browser", support / "BraveSoftware/Brave-Browser"),
            ("brave", "Brave-Browser-Beta", support / "BraveSoftware/Brave-Browser-Beta"),
            ("brave", "Brave-Browser-Nightly", support / "BraveSoftware/Brave-Browser-Nightly"),
            ("chrome", "Chrome", support / "Google/Chrome"),
            ("chrome", "Chrome-Beta", support / "Google/Chrome Beta"),
            ("chrome", "Chrome-Canary", support / "Google/Chrome Canary"),
            ("edge", "Edge", support / "Microsoft Edge"),
            ("edge", "Edge-Beta", support / "Microsoft Edge Beta"),
            ("edge", "Edge-Dev", support / "Microsoft Edge Dev"),
            ("vivaldi", "Vivaldi", support / "Vivaldi"),
            ("chromium", "Chromium", support / "Chromium"),
            ("opera", "Opera", support / "com.operasoftware.Opera"),
            ("firefox", "Firefox", support / "Firefox"),
        ]
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        flatpak = home / ".var" / "app"
        table = [
            ("brave", "Brave-Browser", config / "BraveSoftware/Brave-Browser"),
            ("brave", "Brave-Browser-Beta", config / "BraveSoftware/Brave-Browser-Beta"),
            ("brave", "Brave-Browser-Nightly", config / "BraveSoftware/Brave-Browser-Nightly"),
            ("brave", "Brave-Flatpak", flatpak / "com.brave.Browser/config/BraveSoftware/Brave-Browser"),
            ("brave", "Brave-Snap", home / "snap/brave/current/.config/BraveSoftware/Brave-Browser"),
            ("chrome", "Chrome", config / "google-chrome"),
            ("chrome", "Chrome-Beta", config / "google-chrome-beta"),
            ("chrome", "Chrome-Dev", config / "google-chrome-unstable"),
            ("chrome", "Chrome-Flatpak", flatpak / "com.google.Chrome/config/google-chrome"),
            ("edge", "Edge", config / "microsoft-edge"),
            ("edge", "Edge-Beta", config / "microsoft-edge-beta"),
            ("edge", "Edge-Dev", config / "microsoft-edge-dev"),
            ("edge", "Edge-Flatpak", flatpak / "com.microsoft.Edge/config/microsoft-edge"),
            ("vivaldi", "Vivaldi", config / "vivaldi"),
            ("vivaldi", "Vivaldi-Flatpak", flatpak / "com.vivaldi.Vivaldi/config/vivaldi"),
            ("chromium", "Chromium", config / "chromium"),
            ("chromium", "Chromium-Flatpak", flatpak / "org.chromium.Chromium/config/chromium"),
            ("chromium", "Chromium-Snap", home / "snap/chromium/common/chromium"),
            ("opera", "Opera", config / "opera"),
            ("firefox", "Firefox", home / ".mozilla/firefox"),
            ("firefox", "Firefox-XDG", config / "mozilla/firefox"),  # Firefox 147+ on a fresh install
            ("firefox", "Firefox-Flatpak", flatpak / "org.mozilla.firefox/.mozilla/firefox"),
            ("firefox", "Firefox-Snap", home / "snap/firefox/common/.mozilla/firefox"),
        ]
    return [Root(b, label, path) for b, label, path in table]


def browser_roots() -> list[Root]:
    return [r for r in _candidates() if r.path.is_dir()]


@dataclass(frozen=True)
class Profile:
    key: str  # stable id, e.g. "Brave-Browser/Default" or "Chrome/Profile 1"
    name: str  # what the browser shows, e.g. "Person 1"
    path: Path
    browser: str = "brave"

    @property
    def app(self) -> str:
        return APPS.get(self.browser, self.browser.title())

    @property
    def display(self) -> str:
        return f"{self.app}: {self.name}"

    @property
    def history(self) -> Path:
        return self.path / ("places.sqlite" if self.browser == "firefox" else "History")

    @property
    def cookie_source(self) -> tuple[str, str]:
        """(browser, profile path) for yt-dlp's cookiesfrombrowser."""
        return self.browser, str(self.path)


def _firefox_profiles(root: Root) -> list[Profile]:
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read_string((root.path / "profiles.ini").read_text(encoding="utf-8"))
    except (OSError, configparser.Error):
        return []
    found = []
    for section in ini.sections():
        sec = ini[section]
        if not section.startswith("Profile") or not sec.get("Path"):
            continue
        rel = sec.get("Path")
        path = root.path / rel if sec.get("IsRelative", "1") == "1" else Path(rel)
        if (path / "places.sqlite").is_file():
            found.append(Profile(f"{root.label}/{rel}", sec.get("Name") or path.name, path, root.browser))
    return found


def find_profiles(roots: list[Root] | None = None) -> list[Profile]:
    found: list[Profile] = []
    for root in browser_roots() if roots is None else roots:
        if root.firefox:
            found += _firefox_profiles(root)
            continue
        if (root.path / "History").is_file():  # Opera keeps its one profile in the top folder
            found.append(Profile(root.label, APPS.get(root.browser, root.label), root.path, root.browser))
            continue
        names: dict[str, str] = {}
        try:
            state = json.loads((root.path / "Local State").read_text(encoding="utf-8"))
            for d, meta in (state.get("profile", {}).get("info_cache") or {}).items():
                names[d] = meta.get("name") or d
        except (OSError, ValueError):
            pass
        try:
            dirs = set(names) | {p.name for p in root.path.iterdir()
                                 if p.name == "Default" or p.name.startswith("Profile ")}
        except OSError:
            continue
        for d in sorted(dirs):
            path = root.path / d
            if (path / "History").is_file():
                found.append(Profile(f"{root.label}/{d}", names.get(d, d), path, root.browser))
    return found


def selected_profiles(keys: list[str]) -> list[Profile]:
    """The profiles the user chose to watch; an empty choice means all of them."""
    chosen = set(keys)
    return [p for p in find_profiles() if not chosen or p.key in chosen]


def login_profile(login_key: str, keys: list[str]) -> Profile | None:
    """Whose YouTube login to borrow: the chosen profile, else the first watched one."""
    everything = find_profiles()
    for p in everything:
        if p.key == login_key:
            return p
    chosen = set(keys)
    return next((p for p in everything if not chosen or p.key in chosen), None)


def browsers_label(profiles: list[Profile]) -> str:
    """ "Brave", "Brave and Chrome", "Brave, Chrome and Edge" """
    apps = list(dict.fromkeys(p.app for p in profiles))
    return " and ".join(apps) if len(apps) < 3 else ", ".join(apps[:-1]) + " and " + apps[-1]


@dataclass
class Visit:
    visit_id: int
    video_id: str
    url: str
    title: str
    time: float  # unix seconds


_QUERY = """
SELECT v.id, u.url, u.title, v.visit_time
FROM visits v JOIN urls u ON u.id = v.url
WHERE v.id > ? AND v.visit_time >= ?
  AND (u.url LIKE '%youtube.com/watch%' OR u.url LIKE '%youtu.be/%')
ORDER BY v.id
"""
_NEWEST = "SELECT COALESCE(MAX(id), 0) FROM visits"
# Firefox stores visit times as microseconds since 1970 rather than 1601.
_FIREFOX_QUERY = """
SELECT v.id, p.url, p.title, v.visit_date
FROM moz_historyvisits v JOIN moz_places p ON p.id = v.place_id
WHERE v.id > ? AND v.visit_date >= ?
  AND (p.url LIKE '%youtube.com/watch%' OR p.url LIKE '%youtu.be/%')
ORDER BY v.id
"""
_FIREFOX_NEWEST = "SELECT COALESCE(MAX(id), 0) FROM moz_historyvisits"
_SIDECARS = ("-journal", "-wal")  # Chromium uses a rollback journal, Firefox a write-ahead log


class HistoryReadError(Exception):
    pass


def _open_copy(history: Path, tmp: Path) -> sqlite3.Connection:
    dst = tmp / history.name
    shutil.copyfile(history, dst)
    for suffix in _SIDECARS:
        side = history.with_name(history.name + suffix)
        if side.exists():
            try:
                shutil.copyfile(side, tmp / side.name)
            except OSError:
                pass
    return sqlite3.connect(dst)


def read_visits(history: Path, after_id: int = 0, since: float | None = None) -> tuple[list[Visit], int]:
    """YouTube video visits with id > after_id, plus the newest visit id in the DB."""
    firefox = history.name == "places.sqlite"
    offset = 0 if firefox else WEBKIT_EPOCH_OFFSET
    since_us = int((since + offset) * 1_000_000) if since else 0
    try:
        with tempfile.TemporaryDirectory(prefix="songsnag-") as tmp:
            con = _open_copy(history, Path(tmp))
            try:
                rows = con.execute(_FIREFOX_QUERY if firefox else _QUERY, (after_id, since_us)).fetchall()
                newest = con.execute(_FIREFOX_NEWEST if firefox else _NEWEST).fetchone()[0]
            finally:
                con.close()
    except (OSError, sqlite3.Error) as e:
        raise HistoryReadError(f"{history}: {e}") from e

    visits = []
    for visit_id, url, title, t in rows:
        vid = youtube.video_id(url)
        if vid:
            visits.append(Visit(visit_id, vid, url, youtube.page_title(title), t / 1_000_000 - offset))
    return visits, newest


def history_stamp(history: Path) -> tuple:
    """Cheap change detector so idle polls never copy the database."""
    stamp = []
    for p in (history, *(history.with_name(history.name + s) for s in _SIDECARS)):
        try:
            st = p.stat()
            stamp.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append(None)
    return tuple(stamp)
