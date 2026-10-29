import pytest

from songsnag import albums, collect


class FakeYTM:
    def __init__(self):
        self.albums = {
            "MPREb_lp": {"title": "LP", "type": "Album", "artists": [{"name": "Band"}], "tracks": [
                {"videoId": "lp_track_01", "title": "One", "artists": [{"name": "Band"}]},
                {"videoId": "lp_track_02", "title": "Two (feat. Guest)", "artists": [{"name": "Band"}]},
                {"videoId": None, "title": "Unavailable", "artists": [{"name": "Band"}]}]},
            "MPREb_single": {"title": "Two", "type": "Single", "artists": [{"name": "Band"}], "tracks": [
                {"videoId": "single_two1", "title": "Two", "artists": [{"name": "Band"}]}]},
            "MPREb_new": {"title": "New", "type": "Single", "artists": [{"name": "Band"}], "tracks": [
                {"videoId": "new_track01", "title": "Brand New", "artists": [{"name": "Band"}]}]},
        }
        self.releases = ["MPREb_single", "MPREb_lp"]  # the page lists the single first

    def search(self, query, filter=None, limit=20):
        if filter == "artists":
            return [{"artist": "Band Tribute", "browseId": "UCtribute"}, {"artist": "Band", "browseId": "UCband"}]
        return [{"title": "LP", "browseId": "MPREb_lp", "artists": [{"name": "Band"}]}]

    def get_artist(self, channel_id):
        if channel_id != "UCband":
            return None
        singles = [{"browseId": b, "title": self.albums[b]["title"], "type": "Single"}
                   for b in self.releases if self.albums[b]["type"] == "Single"]
        return {"name": "Band", "albums": {"results": [{"browseId": "MPREb_lp", "title": "LP", "type": "Album"}]},
                "singles": {"results": singles[:1], "browseId": "UCband_s", "params": "more"},
                "_all_singles": singles}

    def get_artist_albums(self, browse_id, params, limit=100):
        return self.get_artist("UCband")["_all_singles"]

    def get_album(self, browse_id):
        return self.albums[browse_id]

    def get_album_browse_id(self, playlist_id):
        return "MPREb_lp"

    def get_playlist(self, playlist_id, limit=100):
        return {"title": "Mix", "tracks": [{"videoId": "lp_track_01", "title": "One", "artists": [{"name": "Band"}],
                                            "album": {"name": "LP"}}]}


@pytest.fixture
def ytm(monkeypatch):
    fake = FakeYTM()
    monkeypatch.setattr(albums, "_yt", lambda: fake)
    monkeypatch.setattr(collect, "_client", lambda: fake)
    return fake


def test_artist_prefers_album_copies_and_skips_unplayable(ytm):
    c = collect.resolve("artist:Band")  # the exact name wins over the first search result
    assert c.kind == "artist" and c.artist_id == "UCband"
    assert [t["video_id"] for t in c.tracks] == ["lp_track_01", "lp_track_02"]  # the single's "Two" is a repeat
    assert c.tracks[0]["album"] == "LP"
    assert set(c.releases) == {"MPREb_lp", "MPREb_single"}


def test_follow_only_skips_the_back_catalog(ytm):
    c = collect.resolve("follow:Band")
    assert c.tracks == [] and c.releases


def test_album_by_search_and_links(ytm):
    assert collect.resolve("album:Band\tLP").name == "LP by Band"
    assert collect.resolve("https://music.youtube.com/browse/MPREb_lp").kind == "album"
    assert collect.resolve("https://music.youtube.com/playlist?list=OLAK5uy_abc").kind == "album"
    assert collect.resolve("https://www.youtube.com/playlist?list=PLmix").kind == "playlist"
    assert collect.resolve("music.youtube.com/channel/UCband").kind == "artist"
    for bad in ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://example.com/x", "artist:  "):
        with pytest.raises(collect.NotFound):
            collect.resolve(bad)


def test_new_releases(ytm):
    ytm.releases.append("MPREb_new")
    releases, tracks = collect.new_releases("UCband", {"MPREb_lp", "MPREb_single"})
    assert "MPREb_new" in releases and [t["video_id"] for t in tracks] == ["new_track01"]


def test_artist_reports_progress_per_album(ytm):
    seen = []
    collect.resolve("artist:Band", progress=lambda done, total, label: seen.append((done, total)))
    assert seen[-1] == (2, 2) and (0, 2) in seen


def test_search_artists_puts_the_exact_name_first(ytm):
    found = collect.search_artists("Band")
    assert [a["id"] for a in found] == ["UCband", "UCtribute"]
    assert collect.resolve("artistid:UCband").artist_id == "UCband"
