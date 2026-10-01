"""Everything that talks to yt-dlp: probing, the music filter, downloading, tagging."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from . import youtube
from .config import Settings
from .tools import find_ffmpeg, find_js_runtimes

log = logging.getLogger(__name__)

COVER_MAX = 600  # px
AUDIO_EXTS = {".m4a", ".opus", ".ogg", ".mp3", ".webm", ".aac", ".flac", ".mka"}

# yt-dlp error text -> how to treat it
PERMANENT = ("video unavailable", "private video", "has been removed", "not available in your country",
             "members-only", "join this channel", "account associated with this video has been terminated",
             "copyright claim", "confirm your age", "inappropriate for some users", "is not a valid url",
             "premieres in", "this live event")
RATE_LIMITED = ("confirm you're not a bot", "confirm you’re not a bot", "http error 429", "rate-limit",
                "rate limit")


class RateLimited(Exception):
    pass


class PermanentError(Exception):
    pass


def classify_error(message: str) -> type[Exception] | None:
    low = message.lower()
    if any(s in low for s in RATE_LIMITED):
        return RateLimited
    if any(s in low for s in PERMANENT):
        return PermanentError
    return None


class _Log:
    """Route yt-dlp's chatter into our log file instead of stdout."""

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg):
        log.debug(msg)

    def warning(self, msg):
        log.info("yt-dlp: %s", msg)

    def error(self, msg):
        log.warning("yt-dlp: %s", msg)


@dataclass
class Result:
    path: Path
    meta: dict


class Downloader:
    """One long-lived YoutubeDL, rebuilt when settings change or cookies break."""

    def __init__(self, settings: Settings, cookie_source: tuple[str, str] | None):
        self.settings = settings
        self.cookie_source = cookie_source  # (browser, profile path) whose YouTube login to use
        self.ffmpeg = find_ffmpeg()
        self.js_runtimes = find_js_runtimes()
        self.cookie_error: str | None = None
        self._ydl: yt_dlp.YoutubeDL | None = None
        self._final_path: str | None = None

    # ---- options ---------------------------------------------------------
    def base_opts(self, cookies: bool) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "logger": _Log(),
            "noplaylist": True,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "js_runtimes": self.js_runtimes or None,
            "windowsfilenames": True,  # names stay valid if the library moves to Windows/exFAT
            "trim_file_name": 180,
            "overwrites": False,
        }
        if self.ffmpeg:
            opts["ffmpeg_location"] = self.ffmpeg
        if cookies and self.cookie_source:
            browser, profile = self.cookie_source
            opts["cookiesfrombrowser"] = (browser, profile, None, None)
        return opts

    def _download_opts(self, cookies: bool) -> dict:
        s = self.settings
        opts = self.base_opts(cookies)
        name = "%(songsnag_name)s.%(ext)s"
        tmpl = f"%(songsnag_artist)s{os.sep}{name}" if s.layout == "artist" else f"%(songsnag_artist)s - {name}"
        opts["paths"] = {"home": str(s.music_path)}
        opts["outtmpl"] = {"default": tmpl}

        if not self.ffmpeg:
            # Without ffmpeg nothing can be remuxed or tagged by yt-dlp; m4a is the most useful raw stream.
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            return opts

        opts["format"] = "bestaudio/best"
        pp_args: dict[str, list[str]] = {}
        pps = []
        if s.embed_cover:
            opts["writethumbnail"] = True
            pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
            # Music uploads use square art letterboxed into 16:9; crop back to the square. Cap the size at
            # 600 px: car stereos and older MP3 players often refuse larger (or progressive) JPEG art.
            crop = "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'," if s.square_cover else ""
            pp_args["thumbnailsconvertor+ffmpeg_o"] = [
                "-c:v", "mjpeg", "-qmin", "1", "-qscale:v", "2",
                "-vf", f"{crop}scale='min({COVER_MAX},iw)':-2"]
        # MP3 is re-encoded at 320 kbps CBR: the most compatible choice, and the highest bitrate keeps the
        # second lossy generation as close to YouTube's stream as MP3 can get.
        quality = "320" if s.audio_format == "mp3" else "0"
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": s.audio_format, "preferredquality": quality,
                    "nopostoverwrites": False})
        if s.audio_format == "mp3":
            pp_args["ffmpegmetadata+ffmpeg_o"] = ["-id3v2_version", "3"]  # v2.4 tags are unreadable on many cars
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False})
        if s.embed_cover:
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        opts["postprocessors"] = pps
        opts["postprocessor_args"] = pp_args
        return opts

    # ---- lifecycle -------------------------------------------------------
    def _open(self) -> yt_dlp.YoutubeDL:
        if self._ydl:
            return self._ydl
        want_cookies = self.settings.use_cookies and bool(self.cookie_source)
        ydl = yt_dlp.YoutubeDL(self._download_opts(want_cookies))
        if want_cookies:
            try:
                ydl.cookiejar  # noqa: B018 - forces the browser cookie load now, so failures surface here
                self.cookie_error = None
            except Exception as e:  # keyring locked, profile moved, unsupported encryption...
                log.warning("could not read browser cookies, continuing without them: %s", e)
                self.cookie_error = str(e)
                ydl.close()
                ydl = yt_dlp.YoutubeDL(self._download_opts(False))
        ydl.add_post_hook(self._on_final)
        self._ydl = ydl
        return ydl

    def close(self) -> None:
        if self._ydl:
            self._ydl.close()
            self._ydl = None

    def _on_final(self, path: str) -> None:
        self._final_path = path

    # ---- work ------------------------------------------------------------
    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DownloadError as e:
            msg = str(e).removeprefix("ERROR: ")
            kind = classify_error(msg)
            if kind:
                raise kind(msg) from e
            raise

    def probe(self, video_id: str, music_hint: bool) -> dict:
        return self._call(self._open().extract_info, youtube.watch_url(video_id, music_hint), download=False)

    def check(self, info: dict, *, music_hint: bool, force: bool) -> str | None:
        """Why this video should not be downloaded, or None to go ahead."""
        if force:
            return None
        if info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            return "live stream"
        s = self.settings
        duration = info.get("duration") or 0
        if s.max_minutes and duration > s.max_minutes * 60:
            return f"too long ({int(duration // 60)} min)"
        if s.music_only and not (music_hint or youtube.is_music(info)):
            cats = ", ".join(info.get("categories") or []) or "no category"
            return f"not music ({cats})"
        return None

    def _unique_name(self, ydl: yt_dlp.YoutubeDL, info: dict, previous: Path | None) -> None:
        """Different videos with the same artist/title get the video id appended.

        `previous` is this video's own earlier file (a re-download), which doesn't count as a clash.
        """
        stem = Path(ydl.prepare_filename(info)).with_suffix("")
        try:
            clash = any(p.stem == stem.name and p.suffix.lower() in AUDIO_EXTS and p != previous
                        for p in stem.parent.iterdir())
        except OSError:
            clash = False
        if clash:
            info["songsnag_name"] = f"{info['songsnag_name']} [{info['id']}]"

    def download(self, info: dict, previous: Path | None = None, album=None) -> Result:
        """Download, convert and tag one video. A `previous` file of the same video is replaced.

        `album` is an albums.AlbumInfo from YouTube Music, when one was found.
        """
        tags = youtube.song_tags(info)
        if album:
            tags.album = album.album
        info["songsnag_artist"] = tags.folder_artist
        info["songsnag_name"] = tags.title
        info["meta_artist"] = tags.artist
        info["meta_title"] = tags.title
        info["meta_album"] = tags.album or None
        # Keep tags tidy: no video description blobs, and "Music" is not a genre.
        info["meta_description"] = info["meta_synopsis"] = ""
        info["meta_genre"] = ", ".join(info.get("genres") or []) or ""
        if tags.album:
            info["meta_album_artist"] = tags.folder_artist
        if album:
            info["meta_album_artist"] = album.album_artist or tags.folder_artist
            if album.year:
                info["meta_date"] = album.year  # the release year, not the video's upload date
            if album.track:
                info["meta_track"] = f"{album.track}/{album.track_total}" if album.track_total else str(album.track)
            if album.cover_url and self.settings.album_art:
                # Offer the album cover as the best thumbnail; yt-dlp falls back to the video's if it fails.
                info["thumbnails"] = [*(info.get("thumbnails") or []),
                                      {"url": album.cover_url, "id": "album", "preference": 1000}]
        elif info.get("release_year"):
            info["meta_date"] = str(info["release_year"])
        info["meta_comment"] = info.get("webpage_url") or youtube.watch_url(info["id"])

        ydl = self._open()
        self.settings.music_path.mkdir(parents=True, exist_ok=True)
        self._unique_name(ydl, info, previous)
        self._final_path = None
        self._call(ydl.process_ie_result, info, download=True)
        if not self._final_path or not Path(self._final_path).exists():
            raise RuntimeError("yt-dlp finished without producing a file")

        fmt = (info.get("requested_formats") or [info])[0]
        path = Path(self._final_path)
        if previous and previous != path and previous.exists():
            previous.unlink()  # replaced by the new download (e.g. .opus -> .mp3)
        codec = path.suffix.lstrip(".")
        abr = fmt.get("abr")
        if self.settings.audio_format == "best" and fmt.get("acodec"):
            codec = fmt["acodec"]  # the stream was kept as-is, so its codec is the honest label
        elif self.settings.audio_format == "mp3" and self.ffmpeg:
            abr = 320
        return Result(path, {
            "title": tags.title,
            "artist": tags.artist,
            "album": tags.album,
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "path": str(path),
            "codec": codec.split(".")[0],
            "abr": abr,
        })

    def account_history(self, limit: int) -> list[tuple[str, str]]:
        """(video_id, title) pairs from the signed-in YouTube watch history."""
        if not self.cookie_source:
            raise RuntimeError("No Brave, Chrome or Edge profile to read your YouTube login from.")
        opts = self.base_opts(cookies=True) | {"extract_flat": "in_playlist", "playlistend": limit}
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = self._call(ydl.extract_info, "https://www.youtube.com/feed/history", download=False)
        out = []
        for e in (res or {}).get("entries") or []:
            vid = e.get("id")
            if vid and youtube.video_id(youtube.watch_url(vid)):
                out.append((vid, e.get("title") or ""))
        return out
