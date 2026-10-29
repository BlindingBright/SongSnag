"""Whole albums, artists and playlists: turning a name or link into the songs to download.

Everything comes from YouTube Music, whose album tracks are the clean "Topic"
uploads with proper metadata. Runs in a worker process (`--resolve`), never in
the tray, so the tray doesn't carry ytmusicapi and requests around.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qs, urlsplit

from . import albums, youtube
from .albums import NOT_THE_ALBUM, Unavailable, norm, song_key

log = logging.getLogger(__name__)

PARALLEL = 4  # albums fetched at once: an artist with 50 releases takes seconds, not half a minute
Progress = Callable[[int, int, str], None]  # (done, total, what's being read)


def _quiet(done: int, total: int, label: str) -> None:
    pass


@dataclass
class Collection:
    kind: str  # album / artist / playlist
    name: str
    tracks: list[dict] = field(default_factory=list)  # video_id, title, artist, album
    artist_id: str = ""  # for artists: what to follow
    releases: list[str] = field(default_factory=list)  # for artists: every release id seen now

    def to_json(self) -> dict:
        return asdict(self)


class NotFound(Exception):
    pass


def _yt():
    return albums._yt()


def _track(t: dict, album_name: str = "", album_id: str = "") -> dict | None:
    vid = t.get("videoId")
    artists = ", ".join(a.get("name", "") for a in t.get("artists") or [] if a.get("name"))
    if not vid or not t.get("title"):
        return None
    album = t.get("album")
    out = {"video_id": vid, "title": t["title"], "artist": artists,
           "album": album_name or (album.get("name", "") if isinstance(album, dict) else album or "")}
    if album_id:  # from a known release: the worker tags it with exactly this album and position
        out["album_id"] = album_id
        out["track"] = t.get("trackNumber")
    return out


def release_tracks(browse_id: str) -> tuple[dict, list[dict]]:
    album = albums._album(browse_id) or {}
    name = album.get("title") or ""
    return album, [t for t in (_track(x, name, browse_id) for x in album.get("tracks") or []) if t]


def album(browse_id: str) -> Collection:
    info, tracks = release_tracks(browse_id)
    if not tracks:
        raise NotFound("That album has no playable songs on YouTube Music.")
    artist = ", ".join(a.get("name", "") for a in info.get("artists") or [])
    return Collection("album", f"{info.get('title')} by {artist}" if artist else info.get("title") or "Album", tracks)


def artist_releases(channel_id: str) -> tuple[str, list[dict]]:
    """(artist name, its albums then its singles and EPs), each {browseId, title, type, year}."""
    a = albums._call(_yt().get_artist, channel_id)
    if not a:
        raise NotFound("YouTube Music doesn't know that artist.")
    releases = []
    for section in ("albums", "singles"):
        sec = a.get(section) or {}
        items = sec.get("results") or []
        if sec.get("params") and sec.get("browseId"):  # the artist page shows only the first few
            items = albums._call(_yt().get_artist_albums, sec["browseId"], sec["params"], limit=None) or items
        releases += [r for r in items if r.get("browseId")]
    return a.get("name") or "", releases


def artist(channel_id: str, *, fetch_tracks: bool = True, progress: Progress = _quiet) -> Collection:
    progress(0, 0, "Reading the artist's page…")
    name, releases = artist_releases(channel_id)
    # Studio albums first, then singles/EPs, compilations last: the first copy of a song is the one kept.
    order = sorted(releases, key=lambda r: albums._rank({"type": r.get("type") or "single", "artists": [],
                                                         "title": r.get("title")}, "") >= NOT_THE_ALBUM)
    out = Collection("artist", name, artist_id=channel_id, releases=[r["browseId"] for r in releases])
    if fetch_tracks:
        out.tracks = unique(tracks_of(order, progress))
    return out


_local = threading.local()


def _client():
    """A YouTube Music client for the current thread (one isn't safe to share between threads)."""
    if not hasattr(_local, "client"):
        from ytmusicapi import YTMusic

        _local.client = YTMusic()
    return _local.client


def _fetch(browse_id: str) -> list[dict]:
    if browse_id not in albums._album_cache:  # the download's album tags reuse it
        albums._album_cache[browse_id] = albums._call(_client().get_album, browse_id) or {}
    album = albums._album_cache[browse_id]
    name = album.get("title") or ""
    return [t for t in (_track(x, name, browse_id) for x in album.get("tracks") or []) if t]


def tracks_of(releases: list[dict], progress: Progress = _quiet) -> list[dict]:
    """Every release's tracks, in release order, fetched a few at a time."""
    total, done = len(releases), 0
    lock = threading.Lock()

    def one(r):
        nonlocal done
        out = _fetch(r["browseId"])
        with lock:
            done += 1
            progress(done, total, r.get("title") or "")
        return out

    progress(0, total, "")
    pool = ThreadPoolExecutor(PARALLEL, thread_name_prefix="album")
    try:
        results = list(pool.map(one, releases))  # map keeps release order: album copies stay first
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # stopped (Cancel / quit): drop what hasn't started
    return [t for chunk in results for t in chunk]


def unique(tracks: list[dict]) -> list[dict]:
    """Drop repeats of the same song (a single that's also on the album, a deluxe edition)."""
    seen, out = set(), []
    for t in tracks:
        key = song_key(t["artist"], t["title"])
        if key not in seen and t["video_id"] not in seen:
            seen |= {key, t["video_id"]}
            out.append(t)
    return out


def playlist(playlist_id: str) -> Collection:
    if playlist_id.startswith("OLAK5uy_"):  # an album's playlist form
        browse_id = albums._call(_yt().get_album_browse_id, playlist_id)
        if browse_id:
            return album(browse_id)
    p = albums._call(_yt().get_playlist, playlist_id, limit=None)
    if not p:
        raise NotFound("That playlist couldn't be read. Private playlists can't be downloaded.")
    tracks = unique([t for t in (_track(x) for x in p.get("tracks") or []) if t])
    if not tracks:
        raise NotFound("That playlist has no playable songs.")
    return Collection("playlist", p.get("title") or "Playlist", tracks)


def _search_artist(query: str) -> str:
    results = albums._call(_yt().search, query, filter="artists", limit=5) or []
    exact = [r for r in results if norm(r.get("artist", "")) == norm(query) and r.get("browseId")]
    pick = (exact or [r for r in results if r.get("browseId")] or [None])[0]
    if not pick:
        raise NotFound(f"No artist called “{query}” on YouTube Music.")
    return pick["browseId"]


def _search_album(query: str, artist_name: str = "") -> str:
    results = albums._call(_yt().search, f"{artist_name} {query}".strip(), filter="albums", limit=10) or []
    results = [r for r in results if r.get("browseId")]

    def fits(r):
        same_title = norm(r.get("title", "")) == norm(query)
        by = [norm(a.get("name", "")) for a in r.get("artists") or []]
        return same_title and (not artist_name or norm(artist_name) in by)

    pick = next((r for r in results if fits(r)), None) or (None if artist_name else (results or [None])[0])
    if not pick:
        raise NotFound(f"Couldn't find the album “{query}” on YouTube Music.")
    return pick["browseId"]


def from_url(url: str, progress: Progress = _quiet) -> Collection:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if not host.endswith(("youtube.com", "youtu.be")):
        raise NotFound("That isn't a YouTube or YouTube Music link.")
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query)
    if path.startswith("/browse/MPREb_"):
        return album(path.split("/")[2])
    if path.startswith("/channel/UC"):
        return artist(path.split("/")[2], progress=progress)
    if query.get("list"):
        return playlist(query["list"][0])
    if path.startswith(("/@", "/c/", "/user/")):
        progress(0, 0, "Finding that channel…")
        cid, name = _channel_of_handle(url)
        try:
            return artist(cid, progress=progress)
        except NotFound:  # a YouTube channel that isn't the artist's YouTube Music page: find it by name
            return artist(_search_artist(name), progress=progress)
    raise NotFound("Paste a link to an album, artist or playlist (not a single song).")


def _channel_of_handle(url: str) -> tuple[str, str]:
    """youtube.com/@handle -> (UC… channel id, channel name). Official artist channels share YouTube Music's ids."""
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "playlistend": 1, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    cid = info.get("channel_id") or ""
    if not cid.startswith("UC"):
        raise NotFound("Couldn't find that channel.")
    return cid, youtube.channel_artist(info.get("channel") or info.get("uploader") or "")


def resolve(spec: str, progress: Progress = _quiet) -> Collection:
    """spec: a link, "artist:NAME", "album:TITLE", or "album:ARTIST\\tTITLE"."""
    kind, _, rest = spec.partition(":")
    if kind in ("http", "https") or spec.startswith(("youtube.com", "music.youtube.com", "www.youtube.com")):
        return from_url(spec if "://" in spec else "https://" + spec, progress)
    rest = rest.strip()
    if not rest:
        raise NotFound("Type a name first.")
    if kind == "artist":
        progress(0, 0, f"Finding {rest} on YouTube Music…")
        return artist(_search_artist(rest), progress=progress)
    if kind == "follow":  # follow only: no back catalog, so skip fetching every album's tracks
        return artist(_search_artist(rest) if not rest.startswith("UC") else rest, fetch_tracks=False)
    if kind == "album":
        artist_name, _, title = rest.rpartition("\t")
        return album(_search_album(title, artist_name))
    raise NotFound(f"Unknown request: {spec}")


def new_releases(channel_id: str, seen: set[str]) -> tuple[list[str], list[dict]]:
    """For a followed artist: (every release id now, songs on releases not seen before)."""
    _, releases = artist_releases(channel_id)
    fresh = [r for r in releases if r["browseId"] not in seen]
    return [r["browseId"] for r in releases], unique(tracks_of(fresh))


__all__ = ["Collection", "NotFound", "Unavailable", "new_releases", "resolve"]
