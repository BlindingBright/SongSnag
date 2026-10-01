import json
import time

from conftest import add_visits, make_history

from songsnag import browsers, config
from songsnag.catalog import PRIORITY_LIVE, Catalog
from songsnag.engine import Engine, parse_takeout

NOW = time.time()


def make_engine(tmp_path, monkeypatch, history, browser="brave"):
    profile = browsers.Profile("Brave-Browser/Default", "Default", history.parent, browser)
    monkeypatch.setattr(browsers, "find_profiles", lambda roots=None: [profile])
    events = []
    eng = Engine(config.load(), Catalog(tmp_path / "c.db"), lambda k, p: events.append((k, p)))
    return eng, events


def test_live_watch_starts_from_now(tmp_path, monkeypatch):
    h = make_history(tmp_path / "Default" / "History",
                     [("https://www.youtube.com/watch?v=oldoldoldol", "Old", NOW - 1000)])
    eng, events = make_engine(tmp_path, monkeypatch, h)
    assert eng.poll_browsers() == 0  # first poll only sets the cursor
    add_visits(h, [("https://music.youtube.com/watch?v=newnewnewne", "New - YouTube Music", NOW)])
    assert eng.poll_browsers() == 1
    job = eng.catalog.next_job()
    assert job["video_id"] == "newnewnewne" and job["priority"] == PRIORITY_LIVE and job["music_hint"] == 1
    assert ("queued", 1) in events
    assert eng.poll_browsers() == 0  # unchanged file: no copy, nothing new


def test_history_cleared(tmp_path, monkeypatch):
    h = make_history(tmp_path / "Default" / "History",
                     [(f"https://www.youtube.com/watch?v=vid{i:08d}", "x", NOW - 5000) for i in range(5)])
    eng, _ = make_engine(tmp_path, monkeypatch, h)
    eng.poll_browsers()
    h.unlink()
    make_history(h, [("https://www.youtube.com/watch?v=aftercleara", "x", NOW)])
    assert eng.poll_browsers() == 1


def test_import_browser_history(tmp_path, monkeypatch):
    h = make_history(tmp_path / "Default" / "History", [
        ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "A", NOW - 3 * 86400),
        ("https://www.youtube.com/watch?v=bbbbbbbbbbb", "B", NOW - 60 * 86400),
    ])
    eng, _ = make_engine(tmp_path, monkeypatch, h)
    assert eng.import_browser_history(30) == 1
    assert eng.import_browser_history(None) == 1
    assert eng.import_browser_history(None) == 0


def test_takeout_json(tmp_path):
    f = tmp_path / "watch-history.json"
    f.write_text(json.dumps([
        {"header": "YouTube Music", "title": "Watched Song", "titleUrl": "https://music.youtube.com/watch?v=aaaaaaaaaaa",
         "time": "2025-01-02T03:04:05.678Z"},
        {"header": "YouTube", "title": "Watched Vid", "titleUrl": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
         "time": "2025-01-01T00:00:00Z"},
        {"header": "YouTube", "title": "Visited channel", "titleUrl": "https://www.youtube.com/channel/x"},
    ]))
    rows = parse_takeout(f)
    assert [(v, t, m) for v, t, _, m in rows] == [("aaaaaaaaaaa", "Song", True), ("bbbbbbbbbbb", "Vid", False)]


def test_takeout_html(tmp_path):
    f = tmp_path / "watch-history.html"
    cell = ('<div class="outer-cell mdl-cell"><div class="header-cell"><p class="mdl-typography--title">{h}<br></p>'
            '</div><div class="content-cell">Watched&nbsp;<a href="{u}">{t}</a><br></div></div>')
    f.write_text("<html><body>"
                 + cell.format(h="YouTube Music", u="https://www.youtube.com/watch?v=aaaaaaaaaaa", t="Tom &amp; Jerry")
                 + cell.format(h="YouTube", u="https://www.youtube.com/watch?v=bbbbbbbbbbb&amp;t=1", t="B")
                 + "</body></html>")
    rows = parse_takeout(f)
    assert [(v, t, m) for v, t, _, m in rows] == [("aaaaaaaaaaa", "Tom & Jerry", True), ("bbbbbbbbbbb", "B", False)]


def test_sources_name_the_browser(tmp_path, monkeypatch):
    h = make_history(tmp_path / "Default" / "History", [])
    eng, _ = make_engine(tmp_path, monkeypatch, h, browser="edge")
    eng.poll_browsers()
    add_visits(h, [("https://www.youtube.com/watch?v=edgeplay001", "x", NOW)])
    eng.poll_browsers()
    add_visits(h, [("https://www.youtube.com/watch?v=edgeold0001", "x", NOW - 86400)])
    eng.import_browser_history(7)
    assert eng.catalog.get("edgeplay001")["source"] == "edge"
    assert eng.catalog.get("edgeold0001")["source"] == "edge-history"
