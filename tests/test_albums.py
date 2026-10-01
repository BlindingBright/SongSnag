import pytest

from songsnag import albums


class FakeYTMusic:
    def __init__(self, songs, albums_by_id):
        self.songs, self.albums = songs, albums_by_id
        self.album_calls = 0

    def search(self, query, filter=None, limit=20):
        return self.songs

    def get_album(self, browse_id):
        self.album_calls += 1
        return self.albums[browse_id]


def song(title, artist, album_id, album_name, seconds, vid="v1"):
    return {"title": title, "artists": [{"name": artist}], "album": {"id": album_id, "name": album_name},
            "duration_seconds": seconds, "videoId": vid}


def album(title, kind, year, tracks, artist="Band", thumb="https://lh3.googleusercontent.com/x=w544-h544-l90-rj"):
    return {"title": title, "type": kind, "year": year, "artists": [{"name": artist}], "trackCount": len(tracks),
            "tracks": tracks, "thumbnails": [{"url": thumb.replace("544", "60"), "width": 60},
                                             {"url": thumb, "width": 544}]}


@pytest.fixture
def fake(monkeypatch):
    def install(songs, albums_by_id):
        client = FakeYTMusic(songs, albums_by_id)
        monkeypatch.setattr(albums, "_yt", lambda: client)
        return client
    return install


def test_prefers_the_album_over_the_single(fake):
    fake([song("Hit", "Band", "S", "Hit", 200, "s1"), song("Hit", "Band", "A", "The LP", 201, "a1")],
         {"S": album("Hit", "Single", "2001", [{"videoId": "s1", "trackNumber": 1, "title": "Hit"}]),
          "A": album("The LP", "Album", "2001", [{"videoId": "x", "trackNumber": 1, "title": "Intro"},
                                                  {"videoId": "a1", "trackNumber": 2, "title": "Hit"}])})
    info = albums.lookup("Band", "Hit", 205)
    assert (info.album, info.kind, info.year, info.track, info.track_total) == ("The LP", "Album", "2001", 2, 2)
    assert info.cover_url == "https://lh3.googleusercontent.com/x=w600-h600-l90-rj"


def test_single_is_used_when_there_is_no_album(fake):
    fake([song("Hit", "Band", "S", "Hit", 200)], {"S": album("Hit", "Single", "2001", [])})
    assert albums.lookup("Band", "Hit", 200).album == "Hit"


def test_compilations_are_never_the_album(fake):
    fake([song("Hit", "Band", "C", "Greatest Hits", 200), song("Hit", "Band", "V", "Party 2002", 200)],
         {"C": album("Greatest Hits", "Album", "2010", []),
          "V": album("Party 2002", "Album", "2002", [], artist="Various Artists")})
    assert albums.lookup("Band", "Hit", 200) is None


@pytest.mark.parametrize("title,artist,duration", [
    ("Hit (Some DJ Remix)", "Band", 200),  # a remix isn't the album track
    ("Hit", "Other Band", 200),  # a cover isn't either
    ("Hit", "Band", 120),  # much shorter than the release: a different edit
])
def test_strict_matching(fake, title, artist, duration):
    fake([song("Hit", "Band", "A", "The LP", 200)], {"A": album("The LP", "Album", "2001", [])})
    assert albums.lookup(artist, title, duration) is None


def test_lenient_on_video_extras(fake):
    fake([song("Hit (Radio Edit)", "Bänd", "A", "The LP", 200)], {"A": album("The LP", "Album", "2001", [])})
    assert albums.lookup("Band", "Hit (feat. Guest)", 260)  # longer video intro, accents, feat., radio edit


def test_album_hint_only_fills_in_details(fake):
    client = fake([song("Hit", "Band", "S", "Hit", 200), song("Hit", "Band", "A", "The LP", 200)],
                  {"S": album("Hit", "Single", "2001", []), "A": album("The LP", "Album", "1999", [])})
    assert albums.lookup("Band", "Hit", 200, album_hint="The LP").year == "1999"
    assert albums.lookup("Band", "Hit", 200, album_hint="Something Else") is None
    assert client.album_calls == 1  # cached across lookups


def test_network_errors_surface_as_unavailable(monkeypatch):
    import requests

    class Down:
        def search(self, *a, **k):
            raise requests.ConnectionError("no route")

    monkeypatch.setattr(albums, "_yt", lambda: Down())
    with pytest.raises(albums.Unavailable):
        albums.lookup("Band", "Hit", 200)
