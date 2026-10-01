"""YouTube URL parsing and turning video metadata into clean song tags."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id(url: str) -> str | None:
    """The 11-char id of a playable YouTube / YouTube Music video URL, else None.

    Shorts, embeds and channel pages are ignored on purpose: a short is a clip of a
    song, not the song, and embeds play on other sites.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    cand = None
    if host in ("youtu.be", "www.youtu.be"):
        cand = parts.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if parts.path == "/watch":
            cand = parse_qs(parts.query).get("v", [None])[0]
    return cand if cand and _ID.match(cand) else None


def is_music_url(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower() == "music.youtube.com"
    except ValueError:
        return False


def watch_url(vid: str, music: bool = False) -> str:
    host = "music.youtube.com" if music else "www.youtube.com"
    return f"https://{host}/watch?v={vid}"


def page_title(title: str | None) -> str:
    """Strip the ' - YouTube' suffix the browser stores in history."""
    t = re.sub(r"^\(\d+\+?\)\s+", "", (title or "").strip())  # "(13) Song" = unread notification count
    for suffix in (" - YouTube Music", " - YouTube"):
        if t.endswith(suffix):
            return t[: -len(suffix)].strip()
    return t


# Bracketed noise in video titles, e.g. "(Official Music Video)", "[Lyrics]", "(HD)".
_NOISE = re.compile(
    r"\s*[\(\[【][^\)\]】]*\b(?:official|lyrics?|visuali[sz]er|music\s*video|video|audio|hd|hq|4k|m/?v)\b[^\)\]】]*[\)\]】]",
    re.I,
)
_TRAIL = re.compile(r"\s*(?:\||//)\s*(?:official|lyrics?|audio|video|hd).*$", re.I)
_SEPARATORS = (" - ", " – ", " — ", " -- ")


def clean_title(title: str) -> str:
    t = _NOISE.sub("", title)
    t = _TRAIL.sub("", t)
    return re.sub(r"\s{2,}", " ", t).strip(" -–—|") or title.strip()


def channel_artist(channel: str | None) -> str:
    c = (channel or "").strip()
    if c.endswith(" - Topic"):
        c = c[: -len(" - Topic")]
    if c.endswith("VEVO") and len(c) > 4:
        c = c[:-4]
    return c.strip()


@dataclass
class Tags:
    artist: str  # all credited artists, for the tag
    folder_artist: str  # primary artist, for the folder name
    title: str
    album: str


def is_music(info: dict) -> bool:
    if "Music" in (info.get("categories") or []):
        return True
    if info.get("track") and (info.get("artist") or info.get("artists")):
        return True
    return (info.get("channel") or info.get("uploader") or "").endswith(" - Topic")


def primary_artist(artist: str) -> str:
    """ "A, B" / "A & B" / "A feat. B" -> "A" (yt-dlp sometimes reports several as one string)."""
    return re.split(r",\s*|\s+&\s+|\s+feat\.?\s+", artist or "", maxsplit=1)[0].strip() or artist


def _norm(text: str) -> str:
    """Compare names ignoring case, accents, spaces and punctuation."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"[\W_]", "", "".join(c for c in decomposed if not unicodedata.combining(c)))


def _same(a: str, b: str) -> bool:
    return bool(a and b) and _norm(a) == _norm(b)


def song_tags(info: dict) -> Tags:
    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    artists = [a.strip() for a in artists if a and a.strip()]
    title = (info.get("track") or "").strip()
    raw = (info.get("title") or info.get("id") or "Unknown").strip()
    channel = channel_artist(info.get("channel") or info.get("uploader"))

    if not title:
        cleaned = clean_title(raw)
        for sep in _SEPARATORS:
            if sep in cleaned:
                left, right = cleaned.split(sep, 1)
                left, right = left.strip(), right.strip()
                if left and right:
                    if _same(right, channel) and not _same(left, channel):
                        left, right = right, left  # "Song - Artist" uploaded by the artist
                    if not artists:
                        artists = [left]
                    title = clean_title(right)
                    break
        title = title or cleaned
    if not artists:
        artists = [channel or "Unknown Artist"]

    return Tags(
        artist=", ".join(dict.fromkeys(artists)),
        folder_artist=primary_artist(artists[0]),
        title=title,
        album=(info.get("album") or "").strip(),
    )
