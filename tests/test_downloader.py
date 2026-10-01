from songsnag import config
from songsnag.downloader import Downloader, PermanentError, RateLimited, classify_error


def dl(**kw):
    s = config.load()
    for k, v in kw.items():
        setattr(s, k, v)
    return Downloader(s, None)


def test_check_filters():
    d = dl(max_minutes=20, music_only=True)
    music = {"categories": ["Music"], "duration": 200}
    assert d.check(music, music_hint=False, force=False) is None
    assert d.check({"categories": ["Gaming"], "duration": 200}, music_hint=False, force=False).startswith("not music")
    assert d.check({"categories": ["Gaming"], "duration": 200}, music_hint=True, force=False) is None
    assert d.check({**music, "duration": 3600}, music_hint=False, force=False) == "too long (60 min)"
    assert d.check({**music, "live_status": "is_live"}, music_hint=False, force=False) == "live stream"
    assert d.check({"categories": ["Gaming"], "duration": 9999}, music_hint=False, force=True) is None
    assert dl(music_only=False, max_minutes=0).check({"duration": 99999}, music_hint=False, force=False) is None


def test_error_classification():
    assert classify_error("[youtube] x: Sign in to confirm you’re not a bot") is RateLimited
    assert classify_error("[youtube] x: Video unavailable. This video is private") is PermanentError
    assert classify_error("HTTP Error 503: Service Unavailable") is None


def test_download_options(tmp_path):
    d = dl(audio_format="best", layout="flat", music_dir=str(tmp_path))
    d.ffmpeg = "/usr/bin/ffmpeg"
    assert "ffmpegmetadata+ffmpeg_o" not in d._download_opts(cookies=False)["postprocessor_args"]
    d.ffmpeg = "/usr/bin/ffmpeg"
    o = d._download_opts(cookies=False)
    assert o["format"] == "bestaudio/best"
    assert o["outtmpl"]["default"] == "%(songsnag_artist)s - %(songsnag_name)s.%(ext)s"
    keys = [p["key"] for p in o["postprocessors"]]
    assert keys == ["FFmpegThumbnailsConvertor", "FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail"]
    assert "cookiesfrombrowser" not in o

    d.ffmpeg = None
    o = d._download_opts(cookies=False)
    assert o["format"].startswith("bestaudio[ext=m4a]") and "postprocessors" not in o

    d = dl()
    d.cookie_source = ("chrome", "/x/Default")
    assert d.base_opts(cookies=True)["cookiesfrombrowser"] == ("chrome", "/x/Default", None, None)


def test_mp3_is_default_and_car_friendly(tmp_path):
    d = dl(music_dir=str(tmp_path))
    assert d.settings.audio_format == "mp3"
    d.ffmpeg = "/usr/bin/ffmpeg"
    o = d._download_opts(cookies=False)
    extract = next(p for p in o["postprocessors"] if p["key"] == "FFmpegExtractAudio")
    assert (extract["preferredcodec"], extract["preferredquality"]) == ("mp3", "320")
    assert o["postprocessor_args"]["ffmpegmetadata+ffmpeg_o"] == ["-id3v2_version", "3"]
    assert "scale='min(600,iw)'" in o["postprocessor_args"]["thumbnailsconvertor+ffmpeg_o"][-1]


def test_redownload_replaces_own_file_but_not_others(tmp_path):
    d = dl(music_dir=str(tmp_path), layout="artist")
    d.ffmpeg = "/usr/bin/ffmpeg"
    ydl = d._open()
    folder = tmp_path / "Artist"
    folder.mkdir()
    own = folder / "Song.opus"
    own.write_bytes(b"x")
    info = {"id": "aaaaaaaaaaa", "ext": "webm", "title": "t", "songsnag_artist": "Artist", "songsnag_name": "Song"}
    d._unique_name(ydl, dict(info), own)
    other = dict(info)
    d._unique_name(ydl, other, None)
    assert other["songsnag_name"] == "Song [aaaaaaaaaaa]"
    same = dict(info)
    d._unique_name(ydl, same, own)
    assert same["songsnag_name"] == "Song"
    d.close()
