import sqlite3
from pathlib import Path

import pytest

from songsnag.browsers import WEBKIT_EPOCH_OFFSET


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SONGSNAG_HOME", str(tmp_path / "songsnag"))
    return tmp_path


@pytest.fixture(autouse=True)
def no_youtube_music(monkeypatch):
    """Tests never talk to YouTube Music; test_albums swaps in a fake client."""
    from songsnag import albums

    def offline():
        raise AssertionError("test tried to reach YouTube Music")

    monkeypatch.setattr(albums, "_yt", offline)
    monkeypatch.setattr(albums, "_album_cache", {})


def webkit(ts: float) -> int:
    return int((ts + WEBKIT_EPOCH_OFFSET) * 1_000_000)


def make_history(path: Path, visits: list[tuple[str, str, float]]) -> Path:
    """A minimal Chromium History DB with (url, title, unix_time) visits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
        CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, visit_duration INTEGER);
    """)
    add_visits(con, visits)
    con.close()
    return path


def add_visits(con_or_path, visits):
    con = sqlite3.connect(con_or_path) if isinstance(con_or_path, Path) else con_or_path
    for url, title, t in visits:
        row = con.execute("SELECT id FROM urls WHERE url=?", (url,)).fetchone()
        uid = row[0] if row else con.execute("INSERT INTO urls(url, title) VALUES(?, ?)", (url, title)).lastrowid
        con.execute("INSERT INTO visits(url, visit_time, visit_duration) VALUES(?, ?, 0)", (uid, webkit(t)))
    con.commit()
    if isinstance(con_or_path, Path):
        con.close()
