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


def test_same_song_from_another_upload_is_skipped(cat, tmp_path):
    saved = tmp_path / "Song.mp3"
    saved.write_bytes(b"x")
    cat.add_sighting("first000001", source="brave", priority=PRIORITY_LIVE)
    cat.next_job()
    cat.finish_done("first000001", {"artist": "Artist", "title": "Song", "path": str(saved)})

    cat.add_sighting("second00001", source="brave", priority=PRIORITY_LIVE)
    assert run_one(cat, "second00001", FakeDownloader({"second00001": "Music"}))[-1] == "skipped"
    assert cat.get("second00001")["note"].startswith("already saved from another upload")

    cat.requeue(["second00001"], force=True)  # "Download anyway" (and format conversions) still download
    assert run_one(cat, "second00001", FakeDownloader({"second00001": "Music"}))[-1] == "downloaded"


def test_one_shot_tasks(cat, monkeypatch, tmp_path):
    from songsnag import collect, discover

    def fake_resolve(spec):
        if spec == "artist:nobody":
            raise collect.NotFound("No artist called nobody")
        return collect.Collection("album", "LP by Band", [{"video_id": "lp_track_01", "title": "One",
                                                           "artist": "Band", "album": "LP"}])

    monkeypatch.setattr(collect, "resolve", fake_resolve)
    events = tmp_path / "e.jsonl"
    assert worker._one_shot(events, worker.resolve("album:Band\tLP")) == 0
    assert worker._one_shot(events, worker.resolve("artist:nobody")) == 1
    lines = [json.loads(line) for line in events.read_text().splitlines()]
    assert lines[0]["kind"] == "resolved" and lines[0]["payload"]["tracks"][0]["video_id"] == "lp_track_01"
    assert lines[1] == {"kind": "task_failed", "payload": "No artist called nobody"}

    assert worker.queue_tracks(cat, lines[0]["payload"]["tracks"], "album") == 1
    r = cat.get("lp_track_01")
    assert r["priority"] == 5 and r["plays"] == 0 and r["music_hint"] == 1 and r["source"] == "album"

    monkeypatch.setattr(discover, "suggest", lambda c: [{"video_id": "sugg0000001", "artist": "X", "title": "Y"}])
    s = config.load()
    s.discover_auto = 1
    config.save(s)
    assert worker._one_shot(events, worker.discover) == 0
    assert cat.get("sugg0000001")["status"] == QUEUED and cat.get_state("discovered_at")

    cat.follow("UCband", "Band", ["MPREb_old"])
    monkeypatch.setattr(collect, "new_releases", lambda cid, seen: (
        ["MPREb_old", "MPREb_new"], [{"video_id": "new_track01", "title": "New", "artist": "Band"}]))
    assert worker._one_shot(events, worker.check_follows) == 0
    assert cat.get("new_track01")["source"] == "followed"
    assert json.loads(cat.follows()[0]["seen"]) == ["MPREb_new", "MPREb_old"]
    assert json.loads(events.read_text().splitlines()[-1])["payload"] == [1, ["Band"]]


def test_queue_tracks_leaves_out_songs_already_saved(cat, tmp_path):
    saved = tmp_path / "Two.mp3"
    saved.write_bytes(b"x")
    cat.add_sighting("video000002", source="brave")
    cat.next_job()
    cat.finish_done("video000002", {"artist": "Band", "title": "Two", "path": str(saved)})
    tracks = [{"video_id": "topic000001", "title": "One", "artist": "Band"},
              {"video_id": "topic000002", "title": "Two (feat. Guest)", "artist": "Band"}]
    assert worker.queue_tracks(cat, tracks, "album") == 1 and cat.get("topic000002") is None


def test_album_downloads_keep_their_release_and_track_number(cat, monkeypatch):
    from songsnag import albums

    tracks = [{"video_id": "topic000003", "title": "Third", "artist": "Band", "album": "LP",
               "album_id": "MPREb_lp", "track": 3}]
    assert worker.queue_tracks(cat, tracks, "album") == 1
    r = cat.get("topic000003")
    assert (r["album_id"], r["track"], r["title"], r["artist"], r["album"]) == ("MPREb_lp", 3, "Third", "Band", "LP")

    asked = []

    def from_release(browse_id, vid, title, artist):
        asked.append((browse_id, vid))
        return albums.AlbumInfo("LP", "Band", "2020", 3, 10)

    monkeypatch.setattr(albums, "from_release", from_release)
    monkeypatch.setattr(albums, "lookup", lambda *a, **k: (_ for _ in ()).throw(AssertionError("guessed")))
    dl = FakeDownloader({"topic000003": "Music"})
    run_one(cat, "topic000003", dl)
    assert asked == [("MPREb_lp", "topic000003")] and dl.album.track == 3
