import json
import time
from pathlib import Path

import pytest

from songsnag import config, paths, worker
from songsnag.catalog import DONE, FAILED, PRIORITY_LIVE, QUEUED, SKIPPED, Catalog
from songsnag.downloader import PermanentError, RateLimited, Result


class FakeDownloader:
    """Stands in for yt-dlp: behaviour per video id."""

    def __init__(self, plan):
        self.plan = plan
        self.cookie_error = None
        self.closed = 0

    def probe(self, vid, music_hint):
        outcome = self.plan[vid]
        if isinstance(outcome, Exception):
            raise outcome
        return {"id": vid, "title": "Artist - Song", "categories": [outcome], "duration": 200}

    def check(self, info, *, music_hint, force):
        return None if info["categories"] == ["Music"] or force else "not music"

    def download(self, info, previous=None, album=None):
        self.album = album
        return Result(Path("/music/Artist/Song.opus"), {"artist": "Artist", "title": "Song", "path": "/m.opus"})

    def close(self):
        self.closed += 1


@pytest.fixture
def cat():
    c = Catalog(paths.data_dir() / "catalog.db")
    yield c
    c.close()


def run_one(cat, vid, dl):
    events = []
    job = cat.next_job()
    assert job["video_id"] == vid
    worker.process(job, dl, config.load(), cat, lambda k, p=None: events.append(k))
    return events


def test_outcomes(cat):
    plan = {"music000001": "Music", "gaming00001": "Gaming", "private0001": PermanentError("Private video"),
            "flaky000001": OSError("connection reset")}
    dl = FakeDownloader(plan)
    for vid in plan:
        cat.add_sighting(vid, source="brave", priority=PRIORITY_LIVE)

    assert run_one(cat, "music000001", dl)[-1] == "downloaded"
    assert cat.get("music000001")["status"] == DONE
    assert run_one(cat, "gaming00001", dl)[-1] == "skipped"
    assert cat.get("gaming00001")["status"] == SKIPPED
    assert run_one(cat, "private0001", dl)[-1] == "failed"
    assert cat.get("private0001")["status"] == FAILED
    run_one(cat, "flaky000001", dl)
    r = cat.get("flaky000001")
    assert r["status"] == QUEUED and r["attempts"] == 1 and r["next_try"] > time.time() and dl.closed == 1


def test_rate_limit_sets_cooldown_without_spending_an_attempt(cat):
    cat.add_sighting("limited0001", source="brave")
    events = run_one(cat, "limited0001", FakeDownloader({"limited0001": RateLimited("confirm you're not a bot")}))
    r = cat.get("limited0001")
    assert events[-1] == "notice" and r["status"] == QUEUED and r["attempts"] == 0
    assert float(cat.get_state(worker.COOLDOWN_KEY)) > time.time() + 1000


def test_run_drains_queue_and_reports(cat, monkeypatch, tmp_path):
    import songsnag.downloader as dlmod

    monkeypatch.setattr(dlmod, "Downloader", lambda settings, profile: FakeDownloader({"music000001": "Music"}))
    cat.add_sighting("music000001", source="brave", priority=PRIORITY_LIVE)
    events = tmp_path / "events.jsonl"
    assert worker.run(events, linger=0) == 0
    kinds = [json.loads(line)["kind"] for line in events.read_text().splitlines()]
    assert kinds == ["busy", "busy", "downloaded", "idle"]
    assert cat.get("music000001")["status"] == DONE


def test_run_respects_pause(cat, monkeypatch, tmp_path):
    s = config.load()
    s.paused = True
    config.save(s)
    cat.add_sighting("music000001", source="brave")
    assert worker.run(tmp_path / "e.jsonl", linger=0) == 0
    assert cat.get("music000001")["status"] == QUEUED


def test_album_lookup_feeds_the_download(cat, monkeypatch):
    from songsnag import albums

    found = albums.AlbumInfo("The Album", "Artist", "2001", 3, 12)
    monkeypatch.setattr(albums, "lookup", lambda artist, title, duration, **kw: found)
    dl = FakeDownloader({"music000001": "Music"})
    cat.add_sighting("music000001", source="brave", priority=PRIORITY_LIVE)
    run_one(cat, "music000001", dl)
    assert dl.album is found
    assert cat.get("music000001")["album_checked"] is not None


def test_album_lookup_outage_does_not_block_download(cat, monkeypatch):
    from songsnag import albums

    def down(*a, **k):
        raise albums.Unavailable("offline")

    monkeypatch.setattr(albums, "lookup", down)
    dl = FakeDownloader({"music000001": "Music"})
    cat.add_sighting("music000001", source="brave", priority=PRIORITY_LIVE)
    assert run_one(cat, "music000001", dl)[-1] == "downloaded"
    assert cat.get("music000001")["album_checked"] is None  # will be retried by "Fill in album info"
