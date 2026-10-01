"""Writing album details into songs already on disk (no re-download)."""

from __future__ import annotations

import base64
import logging
import urllib.request
from pathlib import Path

from .albums import AlbumInfo

log = logging.getLogger(__name__)
SUPPORTED = {".mp3", ".m4a", ".mp4", ".opus", ".ogg"}


def fetch_cover(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except OSError as e:
        log.info("could not fetch cover %s: %s", url, e)
        return None
    return data if _mime(data) else None


def _mime(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def write_album(path: Path, info: AlbumInfo, cover: bytes | None = None) -> None:
    """Set album, album artist, year, track number and (optionally) the front cover."""
    ext = path.suffix.lower()
    if ext == ".mp3":
        _mp3(path, info, cover)
    elif ext in (".m4a", ".mp4"):
        _mp4(path, info, cover)
    elif ext in (".opus", ".ogg"):
        _ogg(path, info, cover)
    else:
        raise ValueError(f"can't tag {ext} files")


def _mp3(path: Path, info: AlbumInfo, cover: bytes | None) -> None:
    from mutagen.id3 import APIC, ID3, TALB, TDRC, TPE2, TRCK, ID3NoHeaderError

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    enc = 1  # UTF-16: valid in ID3v2.3, which car stereos read (v2.4's UTF-8 often isn't)
    tags.setall("TALB", [TALB(encoding=enc, text=info.album)])
    tags.setall("TPE2", [TPE2(encoding=enc, text=info.album_artist)])
    if info.year:
        tags.setall("TDRC", [TDRC(encoding=enc, text=info.year)])
    if info.track:
        tags.setall("TRCK", [TRCK(encoding=enc, text=_track(info))])
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=enc, mime=_mime(cover), type=3, desc="Cover", data=cover))
    tags.save(path, v2_version=3)


def _mp4(path: Path, info: AlbumInfo, cover: bytes | None) -> None:
    from mutagen.mp4 import MP4, MP4Cover

    f = MP4(path)
    f["\xa9alb"] = [info.album]
    f["aART"] = [info.album_artist]
    if info.year:
        f["\xa9day"] = [info.year]
    if info.track:
        f["trkn"] = [(info.track, info.track_total or 0)]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if _mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        f["covr"] = [MP4Cover(cover, imageformat=fmt)]
    f.save()


def _ogg(path: Path, info: AlbumInfo, cover: bytes | None) -> None:
    import mutagen
    from mutagen.flac import Picture

    f = mutagen.File(path)
    if f is None or f.tags is None:
        raise ValueError(f"not an Ogg file: {path}")
    f["ALBUM"] = [info.album]
    f["ALBUMARTIST"] = [info.album_artist]
    if info.year:
        f["DATE"] = [info.year]
    if info.track:
        f["TRACKNUMBER"] = [str(info.track)]
        if info.track_total:
            f["TRACKTOTAL"] = [str(info.track_total)]
    if cover:
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, _mime(cover), "Cover", cover
        f["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
    f.save()


def _track(info: AlbumInfo) -> str:
    return f"{info.track}/{info.track_total}" if info.track_total else str(info.track)
