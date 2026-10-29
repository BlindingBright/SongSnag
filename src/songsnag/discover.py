"""Suggesting new music from what you already listen to.

The songs you play most (weighted toward recent plays) seed YouTube Music
"radio" queues. A suggestion has to turn up from at least two of your songs, or
be by an artist you already play: one radio on its own drifts fast (a parody
song's radio reaches cartoon theme tunes within a few tracks). Anything already
in the catalog, or turned down before, is left out. Runs in a worker process.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict

from . import albums, youtube
from .albums import song_key

log = logging.getLogger(__name__)

SEEDS = 15
PER_SEED = 30
MAX_PER_ARTIST = 3
HALF_LIFE = 60 * 86400  # a play two months ago counts half as much as one today
PAUSE = 0.3


def _seconds(length: str | None) -> float | None:
    try:
        parts = [int(x) for x in (length or "").split(":")]
    except ValueError:
        return None
    return float(sum(v * 60 ** i for i, v in enumerate(reversed(parts)))) if parts else None


def seeds(rows, now: float | None = None, n: int = SEEDS) -> list[tuple[dict, float]]:
    """The songs that best describe your taste, at most two per artist: [(row, weight)]."""
    now = now or time.time()
    scored = []
    for r in rows:
        age = max(0.0, now - (r["last_played"] or now))
        scored.append((dict(r), math.log1p(r["plays"] or 1) * 0.5 ** (age / HALF_LIFE)))
    scored.sort(key=lambda x: -x[1])
    per_artist: dict[str, int] = defaultdict(int)
    out = []
    for r, w in scored:
        a = youtube.loose(youtube.primary_artist(r["artist"] or ""))
        if per_artist[a] >= 2:
            continue
        per_artist[a] += 1
        out.append((r, w))
        if len(out) >= n:
            break
    return out


def suggest(catalog, limit: int = 40, n_seeds: int = SEEDS) -> list[dict]:
    rows = catalog.listening()
    picked = seeds(rows, n=n_seeds)
    if not picked:
        return []
    known_ids, known_keys = catalog.known()
    liked = {youtube.loose(youtube.primary_artist(r["artist"] or "")) for r in rows}
    cands: dict[str, dict] = {}
    for i, (seed, weight) in enumerate(picked):
        if i:
            time.sleep(PAUSE)
        queue = albums._call(albums._yt().get_watch_playlist, videoId=seed["video_id"], radio=True,
                             limit=PER_SEED) or {}
        tracks = [t for t in queue.get("tracks") or [] if t.get("videoId") != seed["video_id"]]
        for pos, t in enumerate(tracks):
            vid, title = t.get("videoId"), t.get("title")
            names = [a.get("name", "") for a in t.get("artists") or [] if a.get("name")]
            if not vid or not title or not names or "PODCAST" in (t.get("videoType") or ""):
                continue
            artist = ", ".join(names)
            key = song_key(artist, title)
            if vid in known_ids or key in known_keys:
                continue
            c = cands.setdefault(key, {"video_id": vid, "title": title, "artist": artist,
                                       "album": (t.get("album") or {}).get("name", "") if isinstance(
                                           t.get("album"), dict) else "",
                                       "duration": _seconds(t.get("length")), "score": 0.0, "seeds": {}})
            c["score"] += weight * (1 - pos / (2 * max(1, len(tracks))))
            c["seeds"][seed["video_id"]] = youtube.primary_artist(seed["artist"] or "")

    out, per_artist = [], defaultdict(int)
    for c in sorted(cands.values(), key=lambda c: -c["score"]):
        lead = youtube.loose(youtube.primary_artist(c["artist"]))
        by_liked = lead in liked
        if len(c["seeds"]) < 2 and not by_liked:
            continue  # only one radio led here: too likely to be drift
        if per_artist[lead] >= MAX_PER_ARTIST:
            continue
        per_artist[lead] += 1
        because = list(dict.fromkeys(c.pop("seeds").values()))  # the artists of the songs that led here
        if by_liked and len(because) < 2:
            c["reason"] = f"More from {youtube.primary_artist(c['artist'])}"
        else:
            c["reason"] = "Like " + " and ".join(because[:2]) + (f" (+{len(because) - 2})" if len(because) > 2
                                                                 else "")
        out.append(c)
        if len(out) >= limit:
            break
    return out
