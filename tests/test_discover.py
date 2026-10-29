import time

import pytest

from songsnag import albums, discover
from songsnag.catalog import Catalog


def track(vid, title, artist):
    return {"videoId": vid, "title": title, "artists": [{"name": artist}], "length": "3:30"}


class FakeYTM:
    radios = {
        "seed_a00001": [track("seed_a00001", "A1", "Alpha"), track("shared00001", "Shared", "Gamma"),
                        track("drift000001", "Cartoon Theme", "Kids TV"), track("more_alpha1", "A9", "Alpha")],
        "seed_b00001": [track("shared00001", "Shared", "Gamma"), track("known000001", "Old", "Beta")],
    }

    def get_watch_playlist(self, videoId=None, radio=False, limit=25):
        return {"tracks": self.radios.get(videoId, [])}


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setattr(albums, "_yt", lambda: FakeYTM())
    monkeypatch.setattr(discover, "PAUSE", 0)
    c = Catalog(tmp_path / "c.db")
    for vid, artist, title in (("seed_a00001", "Alpha", "A1"), ("seed_b00001", "Beta", "B1"),
                               ("known000001", "Beta", "Old")):
        c.add_sighting(vid, source="brave")
        c.next_job()
        c.finish_done(vid, {"artist": artist, "title": title})
    yield c
    c.close()


def test_suggestions_need_two_seeds_or_a_liked_artist(cat):
    got = {s["video_id"]: s for s in discover.suggest(cat)}
    assert set(got) == {"shared00001", "more_alpha1"}  # the cartoon came from one radio only: drift
    assert got["shared00001"]["reason"] in ("Like Alpha and Beta", "Like Beta and Alpha")
    assert got["more_alpha1"]["reason"] == "More from Alpha"
    assert got["shared00001"]["duration"] == 210


def test_dismissed_and_queued_are_not_suggested_again(cat):
    cat.replace_suggestions(discover.suggest(cat))
    cat.answer_suggestions(["shared00001"], "dismissed")
    cat.answer_suggestions(["more_alpha1"], "queued")
    assert discover.suggest(cat) == []


def test_seeds_favour_recent_and_frequent_plays_two_per_artist():
    now = time.time()
    rows = [{"video_id": f"v{i}", "artist": "Same", "title": str(i), "plays": 5, "last_played": now} for i in range(4)]
    rows += [{"video_id": "old", "artist": "Old", "title": "x", "plays": 50, "last_played": now - 720 * 86400},
             {"video_id": "new", "artist": "New", "title": "y", "plays": 3, "last_played": now}]
    picked = [r["video_id"] for r, _ in discover.seeds(rows, now)]
    assert picked[:2] == ["v0", "v1"] and "v2" not in picked and picked.index("new") < picked.index("old")
