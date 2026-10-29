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


def test_no_js_runtime_still_builds_a_downloader(tmp_path):
    import yt_dlp

    d = dl(music_dir=str(tmp_path))
    d.js_runtimes = {}
    assert "js_runtimes" not in d.base_opts(cookies=False)
    yt_dlp.YoutubeDL(d._download_opts(cookies=False)).close()  # must not raise


def test_artist_folders_merge_across_spellings(tmp_path):
    from songsnag.downloader import existing_folder, folder_name

    assert folder_name('"Weird Al" Yankovic') == "'Weird Al' Yankovic"  # not yt-dlp's full-width ＂
    (tmp_path / "＂Weird Al＂ Yankovic").mkdir()
    for spelling in ("'Weird' Al Yankovic", "Weird Al Yankovic", "'Weird Al' Yankovic"):
        assert existing_folder(tmp_path, spelling) == "＂Weird Al＂ Yankovic"
    assert existing_folder(tmp_path, "Someone Else") == "Someone Else"
    assert existing_folder(tmp_path / "missing", "X") == "X"


def test_album_songs_take_the_albums_title_credits_and_folder(tmp_path):
    from songsnag.albums import AlbumInfo

    d = dl(music_dir=str(tmp_path), layout="artist")

    class FakeYDL:
        def prepare_filename(self, info):
            return str(tmp_path / info["songsnag_artist"] / f"{info['songsnag_name']}.mp3")

        def process_ie_result(self, info, download=True):
            self.info = info
            out = tmp_path / info["songsnag_artist"] / f"{info['songsnag_name']}.mp3"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            d._on_final(str(out))

    ydl = FakeYDL()
    d._open = lambda: ydl
    video = {"id": "omv00000001", "title": "DAMAG3 FEAT Lil Broccoli - 5318008 OFFICIAL VIDEO", "channel": "DAMAG3"}
    album = AlbumInfo("TH3 PR3S3NT MOM3NT", "DAMAG3", "2024", 7, 12, artist="DAMAG3", title="5318008 (feat. X)",
                      credits="Amber Ryann, DAMAG3")
    res = d.download(video, None, album)
    assert res.path == tmp_path / "DAMAG3" / "5318008 (feat. X).mp3"  # album artist's folder, album's title
    assert (res.meta["artist"], res.meta["track"]) == ("Amber Ryann, DAMAG3", 7)
    assert ydl.info["meta_track"] == "7/12" and ydl.info["meta_album_artist"] == "DAMAG3"


def test_redownload_under_a_new_folder_removes_the_empty_old_one(tmp_path):
    d = dl(music_dir=str(tmp_path), layout="artist")
    old = tmp_path / "AngelVision" / "Night Friends.mp3"
    old.parent.mkdir()
    old.write_bytes(b"old")

    class FakeYDL:
        def prepare_filename(self, info):
            return str(tmp_path / info["songsnag_artist"] / f"{info['songsnag_name']}.mp3")

        def process_ie_result(self, info, download=True):
            out = Path(self.prepare_filename(info))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"new")
            d._on_final(str(out))

    from pathlib import Path

    d._open = lambda: FakeYDL()
    res = d.download({"id": "krJKCMV-ujo", "title": "Switch Angel - Night Friends"}, previous=old)
    assert res.path == tmp_path / "Switch Angel" / "Night Friends.mp3"
    assert not old.parent.exists()
