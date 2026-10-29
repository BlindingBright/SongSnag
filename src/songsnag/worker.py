"""The download worker: a short-lived process that drains the queue, then exits.

The tray starts it (`songsnag --worker --events FILE`) whenever there is work,
so yt-dlp's ~40 MB never sits in memory while you're not playing anything, and
a hung or crashing download can't take the tray down with it.

Progress goes to the tray as JSON lines appended to the events file; the
catalog database carries the actual state.
"""

from __future__ import annotations

import json
import logging
import random
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import albums, browsers, config, paths, tagging, youtube
from .catalog import PRIORITY_BACKFILL, PRIORITY_COLLECTION, PRIORITY_LIVE, Catalog

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RATE_LIMIT_PAUSE = 30 * 60
LINGER = 30  # seconds to stay warm after the queue empties; plays tend to come in runs
COOLDOWN_KEY = "cooldown_until"


class Events:
    def __init__(self, path: Path | None):
        self._f = path.open("a", encoding="utf-8", buffering=1) if path else None

    def __call__(self, kind: str, payload=None) -> None:
        if self._f:
            self._f.write(json.dumps({"kind": kind, "payload": payload}) + "\n")

    def close(self) -> None:
        if self._f:
            self._f.close()


def cookie_source(settings: config.Settings) -> tuple[str, str] | None:
    p = browsers.login_profile(settings.login_profile, settings.profiles)
    return p.cookie_source if p else None


def process(job, dl, settings: config.Settings, catalog: Catalog, emit) -> None:
    """Probe, filter and download one queued video, recording the outcome in the catalog."""
    from .downloader import PermanentError, RateLimited

    vid = job["video_id"]
    emit("busy", f"Checking: {job['seen_title'] or vid}")

    music_dir = settings.music_path
    try:
        music_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        catalog.postpone(vid, 300, f"music folder unavailable: {e}")
        catalog.set_state(COOLDOWN_KEY, str(time.time() + 300))
        emit("error", f"Can't write to {music_dir}: {e}")
        return

    try:
        info = dl.probe(vid, bool(job["music_hint"]))
        if dl.cookie_error and not catalog.get_state("warned_cookies"):
            catalog.set_state("warned_cookies", "1")
            emit("notice", "Couldn't read your browser's YouTube login, so age-restricted and "
                           "Premium-quality downloads may fail. Details are in the log.")
        reason = dl.check(info, music_hint=bool(job["music_hint"]), force=bool(job["force"]))
        if reason:
            log.info("skip %s: %s", vid, reason)
            catalog.finish_skipped(vid, reason, {
                "title": info.get("title"), "channel": info.get("channel"), "duration": info.get("duration")})
            emit("skipped", vid)
            return
        tags = youtube.song_tags(info)
        twin = None if job["force"] else catalog.saved_copy(albums.song_key(tags.artist, tags.title), vid)
        if twin:
            # The same song from another upload (official video vs. Topic track vs. lyric video).
            reason = f"already saved from another upload ({Path(twin['path']).name})"
            log.info("skip %s: %s", vid, reason)
            catalog.finish_skipped(vid, reason, {
                "title": tags.title, "artist": tags.artist, "channel": info.get("channel"),
                "duration": info.get("duration"), "song_key": twin["song_key"]})  # its plays count for the song
            emit("skipped", vid)
            return
        album, looked_up = _find_album(settings, tags, info, job)
        emit("busy", f"Downloading: {tags.artist} – {tags.title}")
        previous = Path(job["path"]) if job["path"] else None  # set when re-downloading, e.g. to change format
        result = dl.download(info, previous, album)
        if looked_up:
            result.meta["album_checked"] = time.time()
    except RateLimited as e:
        log.warning("rate limited by YouTube: %s", e)
        catalog.postpone(vid, RATE_LIMIT_PAUSE, "YouTube rate limit; will retry")
        catalog.set_state(COOLDOWN_KEY, str(time.time() + RATE_LIMIT_PAUSE))
        emit("notice", "YouTube is asking SongSnag to slow down. Downloads will resume in 30 minutes.")
        return
    except PermanentError as e:
        log.info("unavailable %s: %s", vid, e)
        catalog.finish_failed(vid, str(e)[:500], retry_in=None)
        emit("failed", vid)
        return
    except Exception as e:
        attempts = job["attempts"] + 1
        retry = None if attempts >= MAX_ATTEMPTS else 120 * 2 ** attempts
        log.warning("download of %s failed (attempt %d): %s", vid, attempts, e)
        catalog.finish_failed(vid, str(e)[:500], retry_in=retry)
        dl.close()  # a fresh yt-dlp session often cures transient trouble
        emit("failed", vid)
        return

    catalog.finish_done(vid, result.meta)
    log.info("saved %s", result.path)
    emit("downloaded", {"video_id": vid, "live": job["priority"] >= PRIORITY_LIVE, **result.meta})


def _find_album(settings: config.Settings, tags: youtube.Tags, info: dict,
                job=None) -> tuple[albums.AlbumInfo | None, bool]:
    """(album details or None, whether the lookup actually completed)."""
    if job is not None and job["album_id"]:  # queued from a known album: tag it with exactly that one
        try:
            found = albums.from_release(job["album_id"], job["video_id"], tags.title, tags.artist)
            if found:
                return found, True
        except albums.Unavailable as e:
            log.info("album details unavailable for %s: %s", info.get("id"), e)
    if not settings.lookup_albums:
        return None, False
    try:
        # Topic uploads come with YouTube Music's own length, so hold them to it tightly.
        return albums.lookup(tags.folder_artist, tags.title, info.get("duration"), album_hint=tags.album,
                             strict_duration=bool(info.get("track"))), True
    except albums.Unavailable as e:
        log.info("album lookup unavailable for %s: %s", info.get("id"), e)
    except Exception:
        log.exception("album lookup failed for %s", info.get("id"))
    return None, False


def fill_albums(events_path: Path | None, video_ids: list[str] | None) -> int:
    """One-shot: look up and write album details into songs already saved."""
    emit = Events(events_path)
    catalog = Catalog(paths.data_dir() / "catalog.db")
    settings = config.load()
    rows = catalog.needs_album(video_ids)
    emit("albums_started", len(rows))
    found = done = 0
    try:
        for r in rows:
            path = Path(r["path"]) if r["path"] else None
            if not path or not path.exists() or path.suffix.lower() not in tagging.SUPPORTED:
                catalog.set_album(r["video_id"], None)
                continue
            try:
                info = albums.from_release(r["album_id"], r["video_id"], r["title"] or "", r["artist"] or "") \
                    if r["album_id"] else None
                info = info or albums.lookup(youtube.primary_artist(r["artist"] or ""), r["title"] or "",
                                             r["duration"], album_hint=r["album"] or "")
            except albums.Unavailable as e:
                emit("notice", f"YouTube Music isn't reachable right now ({e}). Try again later.")
                break
            if info:
                cover = None
                if info.cover_url and settings.album_art and settings.embed_cover:
                    cover = tagging.fetch_cover(info.cover_url)
                try:
                    tagging.write_album(path, info, cover)
                    found += 1
                except Exception as e:
                    log.warning("could not tag %s: %s", path, e)
                    info = None
            catalog.set_album(r["video_id"], info.album if info else None, info.track if info else None)
            done += 1
            if done % 5 == 0:
                emit("albums_progress", [done, len(rows), found])
        emit("albums_done", [done, found])
        return 0
    finally:
        emit.close()
        catalog.close()


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def run(events_path: Path | None, linger: float = LINGER) -> int:
    from .downloader import Downloader

    emit = Events(events_path)
    catalog = Catalog(paths.data_dir() / "catalog.db", recover=True)
    dl = None
    dl_key = None
    idle_since = time.time()
    settings, stamp = None, None
    try:
        while True:
            now_stamp = _stamp(config.settings_file())
            if settings is None or now_stamp != stamp:  # the tray saves settings; reread only when it has
                settings, stamp = config.load(), now_stamp
            if settings.paused or float(catalog.get_state(COOLDOWN_KEY, "0") or 0) > time.time():
                break
            job = catalog.next_job()
            if job is None:
                if time.time() - idle_since >= linger:
                    break
                time.sleep(1)
                continue
            key = json.dumps(asdict(settings), sort_keys=True)
            if dl is None or key != dl_key:  # settings changed: rebuild yt-dlp with the new options
                if dl:
                    dl.close()
                dl, dl_key = Downloader(settings, cookie_source(settings)), key
            try:
                process(job, dl, settings, catalog, emit)
            except Exception as e:  # never let one bad video stop the queue
                log.exception("unexpected error on %s", job["video_id"])
                catalog.finish_failed(job["video_id"], f"internal error: {e}", retry_in=None)
            idle_since = time.time()
            if job["priority"] < PRIORITY_LIVE:
                time.sleep(random.uniform(2, 6))  # be gentle with YouTube during imports and whole albums
    finally:
        if dl:
            dl.close()
        emit("idle")
        emit.close()
        catalog.close()
    return 0


def import_account(events_path: Path | None, limit: int) -> int:
    """One-shot: read the signed-in YouTube history and queue it."""
    from .downloader import Downloader

    emit = Events(events_path)
    catalog = Catalog(paths.data_dir() / "catalog.db")
    name = "YouTube account history"
    try:
        settings = config.load()
        entries = Downloader(settings, cookie_source(settings)).account_history(limit)
        added = sum(catalog.add_sighting(vid, source="youtube-history", title=title, played_at=0,
                                         priority=PRIORITY_BACKFILL) for vid, title in entries)
        emit("import_done", [name, added])
        return 0
    except Exception as e:
        log.exception("account history import failed")
        emit("import_failed", [name, str(e)])
        return 1
    finally:
        emit.close()
        catalog.close()


def _one_shot(events_path: Path | None, fn) -> int:
    """Run fn(emit, catalog, settings) once, turning failures into a "task_failed" event."""
    emit = Events(events_path)
    catalog = Catalog(paths.data_dir() / "catalog.db")
    try:
        fn(emit, catalog, config.load())
        return 0
    except albums.Unavailable as e:
        emit("task_failed", f"YouTube Music isn't reachable right now ({e}). Try again later.")
    except Exception as e:
        from .collect import NotFound

        if not isinstance(e, NotFound):
            log.exception("task failed")
        emit("task_failed", str(e))
    finally:
        emit.close()
        catalog.close()
    return 1


def resolve(spec: str):
    def run(emit, catalog: Catalog, settings) -> None:
        from . import collect

        emit("resolved", collect.resolve(spec).to_json())

    return run


def check_follows(emit, catalog: Catalog, settings) -> None:
    """Queue the songs on releases followed artists put out since the last check."""
    from . import collect

    added, who = 0, []
    for i, f in enumerate(catalog.follows()):
        if i:
            time.sleep(1)
        try:
            releases, tracks = collect.new_releases(f["artist_id"], set(json.loads(f["seen"] or "[]")))
        except collect.NotFound:
            log.info("followed artist %s (%s) not found any more", f["name"], f["artist_id"])
            continue
        n = queue_tracks(catalog, tracks, "followed")
        catalog.followed_checked(f["artist_id"], releases)
        if n:
            added += n
            who.append(f["name"])
    emit("follows_done", [added, who])


def discover(emit, catalog: Catalog, settings) -> None:
    from . import discover as disc

    found = disc.suggest(catalog)
    catalog.replace_suggestions(found)
    catalog.set_state("discovered_at", str(time.time()))
    auto = [r["video_id"] for r in catalog.suggestions()[: settings.discover_auto]] if settings.discover_auto else []
    if auto:
        catalog.answer_suggestions(auto, "queued")
    emit("discover_done", [len(found), len(auto)])


def queue_tracks(catalog: Catalog, tracks: list[dict], source: str) -> int:
    """Queue songs the user asked for (an album, an artist, new releases). Returns how many are new.

    Songs already saved from another upload are left out here, so they don't each cost a YouTube probe.
    """
    tracks = [t for t in tracks if not catalog.saved_copy(albums.song_key(t["artist"], t["title"]), t["video_id"])]
    added = sum(catalog.add_sighting(t["video_id"], source=source, title=f"{t['artist']} - {t['title']}",
                                     priority=PRIORITY_COLLECTION, music_hint=True, played=False) for t in tracks)
    catalog.set_release(tracks)
    return added


def main(args) -> int:
    # SIGTERM from the tray (pause/quit) should unwind through the finally blocks.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    events = Path(args.events) if args.events else None
    if args.import_account:
        return import_account(events, args.import_account)
    if args.fill_albums:
        return fill_albums(events, args.ids.split(",") if args.ids else None)
    if args.resolve:
        return _one_shot(events, resolve(args.resolve))
    if args.check_follows:
        return _one_shot(events, check_follows)
    if args.discover:
        return _one_shot(events, discover)
    return run(events)
