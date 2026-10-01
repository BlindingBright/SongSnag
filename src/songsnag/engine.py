"""Filling the queue: watching browsers for plays, and importing past history.

Runs on threads inside the tray process; the UI hears about it through the
`emit(kind, payload)` callback, which must be thread-safe.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import browsers, youtube
from .catalog import PRIORITY_BACKFILL, PRIORITY_LIVE, Catalog
from .config import Settings

log = logging.getLogger(__name__)


class Engine:
    """Watches Brave, Chrome and Edge for new YouTube plays and runs history imports.

    Downloading happens in a separate worker process (see worker.py); this class
    only fills the queue, so it never needs yt-dlp.
    """

    def __init__(self, settings: Settings, cat: Catalog, emit: Callable[[str, object], None]):
        self.settings = settings
        self.catalog = cat
        self.emit = emit
        self._stop = threading.Event()
        self._stamps: dict[str, tuple] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._watch_loop, name="watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def settings_changed(self) -> None:
        self._stamps.clear()  # profile choice may have changed

    def profiles(self) -> list[browsers.Profile]:
        return browsers.selected_profiles(self.settings.profiles)

    # ---- watcher ---------------------------------------------------------
    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            if self.settings.watch_enabled:
                try:
                    self.poll_browsers()
                except Exception:
                    log.exception("watcher poll failed")
            self._stop.wait(max(5, self.settings.poll_seconds))

    def poll_browsers(self) -> int:
        added = 0
        for p in self.profiles():
            stamp = browsers.history_stamp(p.history)
            if self._stamps.get(p.key) == stamp:
                continue
            key = f"cursor:{p.key}"
            cursor = self.catalog.get_state(key)
            try:
                if cursor is None:
                    # First sight of this profile: start from now. Older plays are an explicit import.
                    _, newest = browsers.read_visits(p.history, after_id=2**62)
                    self.catalog.set_state(key, str(newest))
                    self._stamps[p.key] = stamp
                    continue
                visits, newest = browsers.read_visits(p.history, after_id=int(cursor))
                if newest < int(cursor):
                    # History was cleared and ids restarted; pick up whatever was played since.
                    visits, newest = browsers.read_visits(p.history, after_id=0, since=time.time() - 3600)
            except browsers.HistoryReadError as e:
                log.info("history not readable yet (%s), retrying next poll", e)
                continue
            for v in visits:
                if self.catalog.add_sighting(v.video_id, source=p.browser, title=v.title, played_at=v.time,
                                             priority=PRIORITY_LIVE, music_hint=youtube.is_music_url(v.url)):
                    added += 1
                    log.info("heard %s (%s) in %s", v.title or v.video_id, v.video_id, p.name)
            self.catalog.set_state(key, str(newest))
            self._stamps[p.key] = stamp
        if added:
            self.emit("queued", added)
        return added

    # ---- imports ---------------------------------------------------------
    def run_import(self, name: str, fn: Callable[[], int]) -> None:
        def body():
            self.emit("import_started", name)
            try:
                n = fn()
            except Exception as e:
                log.exception("%s import failed", name)
                self.emit("import_failed", (name, str(e)))
                return
            self.emit("import_done", (name, n))

        threading.Thread(target=body, name=f"import-{name}", daemon=True).start()

    def import_browser_history(self, days: int | None) -> int:
        since = time.time() - days * 86400 if days else None
        added = 0
        for p in self.profiles():
            visits, _ = browsers.read_visits(p.history, after_id=0, since=since)
            for v in visits:
                added += self.catalog.add_sighting(v.video_id, source=f"{p.browser}-history", title=v.title,
                                                   played_at=v.time, priority=PRIORITY_BACKFILL,
                                                   music_hint=youtube.is_music_url(v.url))
        return added

    def import_takeout(self, path: Path) -> int:
        added = 0
        for vid, title, when, music in parse_takeout(path):
            added += self.catalog.add_sighting(vid, source="takeout", title=title, played_at=when,
                                               priority=PRIORITY_BACKFILL, music_hint=music)
        return added


def parse_takeout(path: Path) -> list[tuple[str, str, float, bool]]:
    """Google Takeout "watch-history" (.json or .html) -> (video_id, title, time, is_yt_music)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    out = []
    if path.suffix.lower() == ".json" or text.lstrip().startswith("["):
        from datetime import datetime

        for item in json.loads(text):
            vid = youtube.video_id(item.get("titleUrl") or "")
            if not vid:
                continue
            title = re.sub(r"^Watched\s+", "", item.get("title") or "")
            try:
                when = datetime.fromisoformat(item["time"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                when = 0.0
            music = item.get("header") == "YouTube Music" or youtube.is_music_url(item.get("titleUrl") or "")
            out.append((vid, title, when, music))
        return out

    import html as htmllib

    for cell in re.split(r'<div class="outer-cell', text)[1:]:
        header = re.search(r'mdl-typography--title">([^<]+)', cell)
        music_header = bool(header and header.group(1).strip() == "YouTube Music")
        m = re.search(r'<a href="([^"]+)">([^<]*)</a>', cell)
        if not m:
            continue
        url = htmllib.unescape(m.group(1))
        vid = youtube.video_id(url)
        if vid:
            out.append((vid, htmllib.unescape(m.group(2)), 0.0, music_header or youtube.is_music_url(url)))
    return out

