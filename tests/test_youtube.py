import pytest

from songsnag import youtube


@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&index=2", "dQw4w9WgXcQ"),
    ("https://music.youtube.com/watch?v=abcdefghijk&list=RDAMVM", "abcdefghijk"),
    ("https://m.youtube.com/watch?v=abcdefghijk", "abcdefghijk"),
    ("https://youtu.be/abcdefghijk?t=10", "abcdefghijk"),
    ("https://www.youtube.com/shorts/abcdefghijk", None),
    ("https://www.youtube.com/embed/abcdefghijk", None),
    ("https://www.youtube.com/", None),
    ("https://www.youtube.com/watch?v=short", None),
    ("https://notyoutube.com/watch?v=abcdefghijk", None),
    ("https://www.youtube.com.evil.example/watch?v=abcdefghijk", None),
    ("not a url", None),
])
def test_video_id(url, vid):
    assert youtube.video_id(url) == vid


def test_page_title():
    assert youtube.page_title("Song - YouTube") == "Song"
    assert youtube.page_title("Song - YouTube Music") == "Song"
    assert youtube.page_title(None) == ""


@pytest.mark.parametrize("raw,clean", [
    ("Artist - Song (Official Music Video)", "Artist - Song"),
    ("Artist - Song [Official Audio]", "Artist - Song"),
    ("Artist - Song (Lyrics)", "Artist - Song"),
    ("Artist - Song (feat. Other) (Official Video)", "Artist - Song (feat. Other)"),
    ("Artist - Song (Live at Wembley)", "Artist - Song (Live at Wembley)"),
    ("Artist - Song | Official Video", "Artist - Song"),
])
def test_clean_title(raw, clean):
    assert youtube.clean_title(raw) == clean


def test_tags_from_music_metadata():
    info = {"title": "whatever", "track": "Song", "artists": ["A", "B"], "album": "LP", "channel": "A - Topic"}
    t = youtube.song_tags(info)
    assert (t.artist, t.folder_artist, t.title, t.album) == ("A, B", "A", "Song", "LP")


def test_tags_parsed_from_video_title():
    t = youtube.song_tags({"title": "Rick Astley - Never Gonna Give You Up (Official Video)", "channel": "Rick"})
    assert (t.artist, t.title) == ("Rick Astley", "Never Gonna Give You Up")


def test_tags_fallback_to_channel():
    t = youtube.song_tags({"title": "Just a Song (Audio)", "channel": "SomeoneVEVO"})
    assert (t.artist, t.title) == ("Someone", "Just a Song")


def test_folder_artist_splits_features():
    t = youtube.song_tags({"title": "x", "track": "Y", "artist": "Main feat. Guest"})
    assert t.folder_artist == "Main"


def test_is_music():
    assert youtube.is_music({"categories": ["Music"]})
    assert youtube.is_music({"channel": "Band - Topic"})
    assert youtube.is_music({"track": "T", "artist": "A"})
    assert not youtube.is_music({"categories": ["Gaming"], "channel": "Streamer"})


def test_page_title_strips_notification_count():
    assert youtube.page_title("(13) Song - YouTube") == "Song"
    assert youtube.page_title("(1999) Film - YouTube") == "Film"  # can't tell; year-like is rare enough


def test_song_first_titles_swap_when_channel_is_artist():
    t = youtube.song_tags({"title": "Breathe - Télépopmusik", "channel": "Telepopmusik"})
    assert (t.artist, t.title) == ("Télépopmusik", "Breathe")
