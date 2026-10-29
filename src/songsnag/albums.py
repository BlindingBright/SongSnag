"""Album, year, track number and official cover art, looked up on YouTube Music.

YouTube only attaches album details to auto-generated "Artist - Topic" uploads.
For everything else SongSnag searches YouTube Music for the same song and reads
the release it belongs to. Matching is strict (same title, same artist,
compatible length): a remix or fan upload tagged with the original album would
be worse than no album at all.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import dataclass

from .youtube import primary_artist

log = logging.getLogger(__name__)

TYPE_RANK = {"album": 0, "ep": 1, "single": 2}
NOT_THE_ALBUM = 10  # compilations and the like are never used as the album tag
_COMPILATION = re.compile(r"\b(greatest hits|best of|the best|collection|anthology|essential|singles|hits|"
                          r"gold|ultimate|definitive|live|tour|karaoke|playlist|now that's|workout)\b", re.I)
COVER_SIZE = 600


class Unavailable(Exception):
    """YouTube Music couldn't be reached; try again later rather than concluding there's no album."""


@dataclass
class AlbumInfo:
    album: str
    album_artist: str
    year: str = ""
    track: int | None = None
    track_total: int | None = None
    kind: str = ""  # Album / EP / Single
    cover_url: str | None = None
    artist: str = ""  # the song's artist as YouTube Music spells it (e.g. with the quotes in "Weird Al")
    title: str = ""  # from a known release: the album's own title and credits for this song,
    credits: str = ""  # cleaner than a music video's ("5318008 OFFICIAL VIDEO" -> "5318008")


_FEAT = re.compile(r"\s*[\(\[]?\s*\b(?:feat|ft|featuring|with)\b\.?\s+[^\)\]]*[\)\]]?", re.I)
_REMASTER = re.compile(r"\s*[\(\[-][^\)\]]*\bremaster(?:ed)?\b[^\)\]]*[\)\]]?", re.I)
# Same recording, different label: a radio-edit video matches its "(Radio Edit)" release.
_EDIT = re.compile(r"\s*[\(\[]\s*(?:radio edit|single edit|single version|edit|album version|original mix)\s*[\)\]]",
                   re.I)


def norm(text: str) -> str:
    """Compare titles/names ignoring case, accents, punctuation, 'feat.' credits and remaster notes."""
    t = _EDIT.sub("", _REMASTER.sub("", _FEAT.sub("", text or "")))
    t = unicodedata.normalize("NFKD", t.casefold().replace("&", "and"))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", t)


# Bracketed notes that make it a different recording; anything else in brackets ("(Parody of …)",
# "(feat. …)", "[Official Video]", "(2011 Remaster)") is the same song.
_DIFFERENT = re.compile(r"\b(live|remix|mix|rmx|acoustic|instrumental|demo|cover|karaoke|reprise|unplugged|"
                        r"sped|slowed|session|orchestral|piano|a cappella|acapella|extended|dub|vip|bootleg)\b", re.I)
_BRACKETED = re.compile(r"\s*(\([^()]*(?:\([^()]*\)[^()]*)*\)|\[[^\[\]]*\])")
KEY_VERSION = "3"  # bump when song_key changes, so saved songs get re-keyed


def song_key(artist: str, title: str) -> str:
    """Identifies a song across uploads: the official video, the Topic track and a lyric video share it."""
    core = _BRACKETED.sub(lambda m: m.group(0) if _DIFFERENT.search(m.group(0)) else "", title or "")
    core = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s+", core, maxsplit=1, flags=re.I)[0]
    return f"{norm(primary_artist(artist or ''))}|{norm(core) or norm(title or '')}"


def duration_ok(ours: float | None, theirs: float | None, strict: bool = False) -> bool:
    if not ours or not theirs:
        return True
    diff = ours - theirs
    if abs(diff) <= 10:
        return True
    # Music videos often add an intro/outro to the album cut, but are never much shorter.
    return not strict and 0 < diff <= 90


_client = None
_client_lock = threading.Lock()
_album_cache: dict[str, dict] = {}


def _yt():
    global _client
    with _client_lock:
        if _client is None:
            from ytmusicapi import YTMusic

            _client = YTMusic()
        return _client


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # ytmusicapi raises requests/HTTP/parse errors of many kinds
        import requests

        if isinstance(e, (requests.RequestException, OSError)):
            raise Unavailable(str(e)) from e
        log.info("YouTube Music gave an unexpected answer (%s: %s)", type(e).__name__, e)
        return None


def _album(browse_id: str) -> dict | None:
    if browse_id not in _album_cache:
        _album_cache[browse_id] = _call(_yt().get_album, browse_id) or {}
    return _album_cache[browse_id] or None


def _rank(album: dict, song_title: str) -> int:
    rank = TYPE_RANK.get((album.get("type") or "").lower(), 3)
    artists = [a.get("name", "") for a in album.get("artists") or []]
    if any(norm(a) == "variousartists" for a in artists):
        rank += NOT_THE_ALBUM
    title = album.get("title") or ""
    if _COMPILATION.search(title) and not _COMPILATION.search(song_title):
        rank += NOT_THE_ALBUM
    return rank


def _cover(album: dict) -> str | None:
    thumbs = album.get("thumbnails") or []
    if not thumbs:
        return None
    url = max(thumbs, key=lambda t: t.get("width") or 0)["url"]
    # Google image URLs carry their size; ask for exactly the size SongSnag embeds.
    return re.sub(r"=w\d+-h\d+", f"=w{COVER_SIZE}-h{COVER_SIZE}", url)


def lookup(artist: str, title: str, duration: float | None = None, *, album_hint: str = "",
           strict_duration: bool = False) -> AlbumInfo | None:
    """The release this song belongs to on YouTube Music, or None without a confident match."""
    if not artist or not title:
        return None
    want_title, want_artist = norm(title), norm(artist)
    results = _call(_yt().search, f"{artist} {_FEAT.sub('', title).strip()}", filter="songs", limit=20) or []

    matches = []
    for r in results:
        album_ref = r.get("album") or {}
        if not album_ref.get("id") or norm(r.get("title", "")) != want_title:
            continue
        if not any(norm(a.get("name", "")) == want_artist for a in r.get("artists") or []):
            continue
        if not duration_ok(duration, r.get("duration_seconds"), strict_duration):
            continue
        matches.append(r)
    if album_hint:  # YouTube already named the album: only fill in the details
        matches = [m for m in matches if norm(m["album"].get("name", "")) == norm(album_hint)]

    best = None
    for song in matches[:4]:
        album = _album(song["album"]["id"])
        if not album:
            continue
        rank = _rank(album, title)
        if rank < NOT_THE_ALBUM and (best is None or rank < best[0]):
            best = (rank, song, album)
        if rank == 0:
            break  # a proper album: nothing will beat it
    if not best:
        return None

    _, song, album = best
    credited = next((a.get("name", "") for a in song.get("artists") or [] if norm(a.get("name", "")) == want_artist),
                    "")
    return _info(album, song.get("videoId"), title, artist, credited, song["album"].get("name", ""))


def _info(album: dict, video_id: str | None, title: str, artist: str, credited: str = "",
          fallback_name: str = "") -> AlbumInfo:
    tracks = album.get("tracks") or []
    number = next((t.get("trackNumber") for t in tracks if video_id and t.get("videoId") == video_id), None)
    if number is None:
        number = next((t.get("trackNumber") for t in tracks if norm(t.get("title", "")) == norm(title)), None)
    return AlbumInfo(
        album=album.get("title") or fallback_name,
        album_artist=", ".join(a.get("name", "") for a in album.get("artists") or []) or artist,
        year=str(album.get("year") or ""),
        track=number,
        track_total=album.get("trackCount") or len(tracks) or None,
        kind=album.get("type") or "",
        cover_url=_cover(album),
        artist=credited,
    )


def from_release(browse_id: str, video_id: str, title: str, artist: str) -> AlbumInfo | None:
    """Details for a song queued from a known album (whole-album/artist downloads): no guessing which release.

    Every song of the album gets the same album name and its own track number, so the album plays in order.
    """
    album = _album(browse_id)
    if not album:
        return None
    lead = norm(primary_artist(artist))
    track = next((t for t in album.get("tracks") or [] if t.get("videoId") == video_id), None)
    names = [a.get("name", "") for a in (track or {}).get("artists") or [] if a.get("name")]
    credited = next((n for n in names if norm(n) == lead), "")
    info = _info(album, video_id, title, artist, credited)
    if track and track.get("title"):
        info.title, info.credits = track["title"], ", ".join(names)
        info.artist = info.artist or (names[0] if names else "")
    return info
