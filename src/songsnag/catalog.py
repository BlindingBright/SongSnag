"""The song catalog: every video seen, its queue state, and where the file landed.

One SQLite file doubles as the persistent download queue, so nothing is lost
if the app is closed mid-download.
"""

from __future__ import annotations

import csv
import sqlite3
import threading
import time
from pathlib import Path

from .albums import KEY_VERSION, song_key

QUEUED, WORKING, DONE, SKIPPED, FAILED = "queued", "working", "done", "skipped", "failed"
STATUSES = (QUEUED, WORKING, DONE, SKIPPED, FAILED)

PRIORITY_LIVE = 10  # played just now
PRIORITY_COLLECTION = 5  # an album, artist or playlist the user asked for
PRIORITY_BACKFILL = 0  # imported from history
PREVIEWED = "previewed in Discover"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    video_id     TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'queued',
    priority     INTEGER NOT NULL DEFAULT 0,
    source       TEXT,
    music_hint   INTEGER NOT NULL DEFAULT 0,
    force        INTEGER NOT NULL DEFAULT 0,
    seen_title   TEXT,
    first_seen   REAL,
    last_played  REAL,
    plays        INTEGER NOT NULL DEFAULT 0,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    channel      TEXT,
    duration     REAL,
    path         TEXT,
    codec        TEXT,
    abr          REAL,
    downloaded_at REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_try     REAL NOT NULL DEFAULT 0,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS tracks_queue ON tracks(status, priority DESC, first_seen);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS follows (
    artist_id    TEXT PRIMARY KEY,  -- YouTube Music channel id
    name         TEXT NOT NULL,
    added        REAL,
    last_checked REAL,
    seen         TEXT NOT NULL DEFAULT '[]'  -- JSON list of release ids already known
);
CREATE TABLE IF NOT EXISTS suggestions (
    video_id     TEXT PRIMARY KEY,
    song_key     TEXT,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    duration     REAL,
    reason       TEXT,
    score        REAL,
    created      REAL,
    state        TEXT NOT NULL DEFAULT 'new'  -- new / queued / dismissed
);
"""


# Every insert and update stamps the row, whichever code path (or process) made it. The WHEN clause keeps the
# trigger's own update from firing it again.
_NOW = "((julianday('now') - 2440587.5) * 86400.0)"
_TRIGGERS = f"""
CREATE TRIGGER IF NOT EXISTS tracks_inserted AFTER INSERT ON tracks BEGIN
    UPDATE tracks SET changed = {_NOW} WHERE video_id = NEW.video_id;
END;
CREATE TRIGGER IF NOT EXISTS tracks_updated AFTER UPDATE ON tracks WHEN NEW.changed IS OLD.changed BEGIN
    UPDATE tracks SET changed = {_NOW} WHERE video_id = NEW.video_id;
END;
CREATE INDEX IF NOT EXISTS tracks_changed ON tracks(changed);
"""


class Catalog:
    def __init__(self, path: Path, recover: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            columns = {r[1] for r in self._db.execute("PRAGMA table_info(tracks)")}
            if "album_checked" not in columns:  # added in 1.1: when the album lookup last ran
                self._db.execute("ALTER TABLE tracks ADD COLUMN album_checked REAL")
            for col, kind in (("album_id", "TEXT"), ("track", "INTEGER")):  # 1.1: the release a song was queued
                if col not in columns:                                     # from, and its track number
                    self._db.execute(f"ALTER TABLE tracks ADD COLUMN {col} {kind}")
            if "changed" not in columns:  # 1.1: when a row last changed, so the Library reloads only those
                self._db.execute("ALTER TABLE tracks ADD COLUMN changed REAL")
            self._db.executescript(_TRIGGERS)
            fresh = "song_key" not in columns
            if fresh:  # added in 1.1: artist+title, to spot the same song from another upload
                self._db.execute("ALTER TABLE tracks ADD COLUMN song_key TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS tracks_song ON tracks(song_key)")
            keyed = self._db.execute("SELECT value FROM state WHERE key='song_key_version'").fetchone()
            if fresh or not keyed or keyed[0] != KEY_VERSION:
                self._db.executemany("UPDATE tracks SET song_key=? WHERE video_id=?", [
                    (song_key(r[1] or "", r[2]), r[0]) for r in self._db.execute(
                        "SELECT video_id, artist, title FROM tracks WHERE status=? AND title IS NOT NULL", (DONE,))])
                self._db.execute("INSERT OR REPLACE INTO state(key, value) VALUES('song_key_version', ?)",
                                 (KEY_VERSION,))
            if recover:
                # Only the download worker passes this: whatever it was doing when it last stopped goes back in line.
                self._db.execute("UPDATE tracks SET status=? WHERE status=?", (QUEUED, WORKING))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _run(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, args)

    def _all(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def version(self) -> tuple[int, int]:
        """Changes whenever anyone (this connection or the worker's) writes; cheap enough to poll."""
        with self._lock:
            return self._db.execute("PRAGMA data_version").fetchone()[0], self._db.total_changes

    # ---- state -----------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        rows = self._all("SELECT value FROM state WHERE key=?", (key,))
        return rows[0][0] if rows else default

    def set_state(self, key: str, value: str) -> None:
        self._run("INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))

    # ---- queue -----------------------------------------------------------
    def add_sighting(self, video_id: str, *, source: str, title: str = "", played_at: float | None = None,
                     priority: int = PRIORITY_BACKFILL, music_hint: bool = False, played: bool = True) -> bool:
        """Record that a video was played/seen. Returns True if it was newly queued.

        played=False queues a video the user asked for without having listened to it (albums, suggestions),
        so it doesn't count as a play.
        """
        now = time.time()
        played_at = played_at or now
        with self._lock, self._db:
            row = self._db.execute("SELECT status, last_played FROM tracks WHERE video_id=?", (video_id,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO tracks(video_id, status, priority, source, music_hint, seen_title, first_seen,"
                    " last_played, plays) VALUES(?,?,?,?,?,?,?,?,?)",
                    (video_id, QUEUED, priority, source, int(music_hint), title, now,
                     played_at if played else None, int(played)))
                return True
            if not played:
                self._db.execute("UPDATE tracks SET music_hint = MAX(music_hint, ?), priority = CASE WHEN status = ?"
                                 " THEN MAX(priority, ?) ELSE priority END WHERE video_id = ?",
                                 (int(music_hint), QUEUED, priority, video_id))
                return False
            newer = played_at > (row["last_played"] or 0) + 1
            self._db.execute(
                "UPDATE tracks SET plays = plays + ?, last_played = MAX(COALESCE(last_played, 0), ?),"
                " music_hint = MAX(music_hint, ?), seen_title = COALESCE(NULLIF(seen_title, ''), ?),"
                " priority = CASE WHEN status = ? THEN MAX(priority, ?) ELSE priority END"
                " WHERE video_id = ?",
                (int(newer), played_at, int(music_hint), title, QUEUED, priority, video_id))
            return False

    def next_job(self) -> sqlite3.Row | None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM tracks WHERE status=? AND next_try<=? ORDER BY priority DESC, first_seen LIMIT 1",
                (QUEUED, time.time())).fetchone()
            if row:
                self._db.execute("UPDATE tracks SET status=? WHERE video_id=?", (WORKING, row["video_id"]))
            return row

    def upcoming(self, limit: int = 6) -> list[sqlite3.Row]:
        """What the worker is on now, then what it will do next (the order next_job() picks them)."""
        return self._all("SELECT * FROM tracks WHERE status=? OR (status=? AND next_try<=?)"
                         " ORDER BY status=? DESC, priority DESC, first_seen LIMIT ?",
                         (WORKING, QUEUED, time.time(), WORKING, limit))

    def pending_count(self) -> int:
        return self._all("SELECT COUNT(*) FROM tracks WHERE status IN (?, ?)", (QUEUED, WORKING))[0][0]

    def runnable_count(self) -> int:
        return self._all("SELECT COUNT(*) FROM tracks WHERE status=? AND next_try<=?", (QUEUED, time.time()))[0][0]

    def next_wakeup(self) -> float | None:
        rows = self._all("SELECT MIN(next_try) FROM tracks WHERE status=?", (QUEUED,))
        return rows[0][0]

    def _meta_sql(self, meta: dict) -> tuple[str, list]:
        cols = [k for k in ("title", "artist", "album", "channel", "duration", "path", "codec", "abr",
                            "album_checked", "song_key", "track") if k in meta]
        return "".join(f", {c}=?" for c in cols), [meta[c] for c in cols]

    def finish_done(self, video_id: str, meta: dict) -> None:
        extra, args = self._meta_sql(meta)
        key = song_key(meta.get("artist") or "", meta.get("title") or "") if meta.get("title") else None
        self._run(f"UPDATE tracks SET status=?, downloaded_at=?, note=NULL, force=0, song_key=?{extra}"
                  " WHERE video_id=?", (DONE, time.time(), key, *args, video_id))

    def saved_copy(self, key: str, exclude: str) -> sqlite3.Row | None:
        """Another video already saved as this same song (artist + title), whose file still exists."""
        for r in self._all("SELECT * FROM tracks WHERE song_key=? AND status=? AND video_id<>?", (key, DONE, exclude)):
            if r["path"] and Path(r["path"]).exists():
                return r
        return None

    def finish_skipped(self, video_id: str, reason: str, meta: dict | None = None) -> None:
        extra, args = self._meta_sql(meta or {})
        self._run(f"UPDATE tracks SET status=?, note=?{extra} WHERE video_id=?", (SKIPPED, reason, *args, video_id))

    def finish_failed(self, video_id: str, error: str, retry_in: float | None) -> None:
        """retry_in=None means give up; otherwise requeue after that many seconds."""
        if retry_in is None:
            self._run("UPDATE tracks SET status=?, note=?, attempts=attempts+1 WHERE video_id=?",
                      (FAILED, error, video_id))
        else:
            self._run("UPDATE tracks SET status=?, note=?, attempts=attempts+1, next_try=? WHERE video_id=?",
                      (QUEUED, error, time.time() + retry_in, video_id))

    def postpone(self, video_id: str, seconds: float, note: str) -> None:
        """Put a job back without counting it as an attempt (e.g. YouTube rate limit)."""
        self._run("UPDATE tracks SET status=?, next_try=?, note=? WHERE video_id=?",
                  (QUEUED, time.time() + seconds, note, video_id))

    def requeue(self, video_ids: list[str], force: bool = True) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE tracks SET status=?, force=?, attempts=0, next_try=0, note=NULL, priority=?"
                " WHERE video_id=? AND status<>?",
                [(QUEUED, int(force), PRIORITY_LIVE, v, WORKING) for v in video_ids])

    def retry_failed(self) -> int:
        return self._run("UPDATE tracks SET status=?, attempts=0, next_try=0 WHERE status=?", (QUEUED, FAILED)).rowcount

    def forget(self, video_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("DELETE FROM tracks WHERE video_id=? AND status<>?", [(v, WORKING) for v in video_ids])

    # ---- browsing --------------------------------------------------------
    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATUSES, 0)
        for status, n in self._all("SELECT status, COUNT(*) FROM tracks GROUP BY status"):
            out[status] = n
        return out

    def get(self, video_id: str) -> sqlite3.Row | None:
        rows = self._all("SELECT * FROM tracks WHERE video_id=?", (video_id,))
        return rows[0] if rows else None

    def recent_downloads(self, limit: int = 10) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM tracks WHERE status=? ORDER BY downloaded_at DESC LIMIT ?", (DONE, limit))

    def search(self, text: str = "", status: str | None = None, limit: int | None = 5000) -> list[sqlite3.Row]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if text:
            like = f"%{text}%"
            where.append("(artist LIKE ? OR title LIKE ? OR seen_title LIKE ? OR album LIKE ? OR video_id=?)")
            args += [like, like, like, like, text]
        sql = "SELECT * FROM tracks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(downloaded_at, first_seen) DESC LIMIT ?"
        return self._all(sql, (*args, -1 if limit is None else limit))  # -1: no limit

    LIBRARY_COLUMNS = ("video_id", "status", "priority", "next_try", "first_seen", "downloaded_at", "artist",
                       "title", "seen_title", "album", "track", "codec", "abr", "plays", "source", "note", "path")

    def library_rows(self, text: str = "", status: str | None = None, since: float | None = None) -> list[tuple]:
        """Everything the Library shows, as plain tuples in LIBRARY_COLUMNS order (the window sorts them).

        since: only rows changed at or after that time (seconds), for cheap incremental reloads.
        """
        where, args = [], []
        if since is not None:
            where.append("changed >= ?")
            args.append(since)
        if status:
            where.append("status=?")
            args.append(status)
        if text:
            like = f"%{text}%"
            where.append("(artist LIKE ? OR title LIKE ? OR seen_title LIKE ? OR album LIKE ? OR video_id=?)")
            args += [like, like, like, like, text]
        sql = f"SELECT {', '.join(self.LIBRARY_COLUMNS)} FROM tracks" + (" WHERE " + " AND ".join(where) if where
                                                                         else "")
        with self._lock:
            cur = self._db.cursor()
            cur.row_factory = None  # plain tuples: several times faster than sqlite3.Row for big libraries
            return cur.execute(sql, args).fetchall()

    def changed_ids(self, since: float) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT video_id FROM tracks WHERE changed >= ?", (since,))}

    def ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT video_id FROM tracks")}

    def done_under(self, folder: Path) -> list[sqlite3.Row]:
        prefix = str(folder.resolve())
        return [r for r in self._all("SELECT * FROM tracks WHERE status=? AND path IS NOT NULL", (DONE,))
                if str(Path(r["path"]).resolve()).startswith(prefix)]

    def needs_album(self, video_ids: list[str] | None = None) -> list[sqlite3.Row]:
        """Saved songs whose album was never looked up (or exactly these, to look them up again)."""
        if video_ids is not None:
            marks = ",".join("?" * len(video_ids))
            return self._all(f"SELECT * FROM tracks WHERE status=? AND video_id IN ({marks})", (DONE, *video_ids))
        return self._all("SELECT * FROM tracks WHERE status=? AND album_checked IS NULL"
                         " ORDER BY downloaded_at DESC", (DONE,))

    def set_release(self, tracks: list[dict]) -> None:
        """Where queued songs came from (album id, track number) and what they are, so the Library can show them
        properly before they download; the download's own tags replace these."""
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE tracks SET album_id=COALESCE(?, album_id), track=COALESCE(?, track),"
                " artist=COALESCE(artist, ?), title=COALESCE(title, ?), album=COALESCE(NULLIF(album, ''), ?)"
                " WHERE video_id=? AND status<>?",
                [(t.get("album_id"), t.get("track"), t.get("artist") or None, t.get("title") or None,
                  t.get("album") or None, t["video_id"], DONE) for t in tracks])

    def set_album(self, video_id: str, album: str | None, track: int | None = None) -> None:
        if album:
            self._run("UPDATE tracks SET album=?, album_checked=?, track=COALESCE(?, track) WHERE video_id=?",
                      (album, time.time(), track, video_id))
        else:
            self._run("UPDATE tracks SET album_checked=? WHERE video_id=?", (time.time(), video_id))

    def set_path(self, video_id: str, path: str) -> None:
        self._run("UPDATE tracks SET path=? WHERE video_id=?", (path, video_id))

    # ---- export ----------------------------------------------------------
    def export_csv(self, dest: Path) -> int:
        rows = self._all("SELECT * FROM tracks ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE")
        cols = ["video_id", "status", "artist", "title", "album", "duration", "codec", "abr", "plays",
                "source", "path", "note"]
        with dest.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols + ["url"])
            for r in rows:
                w.writerow([r[c] for c in cols] + [f"https://www.youtube.com/watch?v={r['video_id']}"])
        return len(rows)

    def export_m3u(self, dest: Path) -> int:
        rows = self._all("SELECT * FROM tracks WHERE status=? AND path IS NOT NULL ORDER BY artist COLLATE NOCASE,"
                         " album COLLATE NOCASE, track, title COLLATE NOCASE", (DONE,))  # albums in order
        n = 0
        with dest.open("w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for r in rows:
                p = Path(r["path"])
                if not p.exists():
                    continue
                try:
                    ref = p.relative_to(dest.parent)
                except ValueError:
                    ref = p
                f.write(f"#EXTINF:{int(r['duration'] or -1)},{r['artist']} - {r['title']}\n{ref.as_posix()}\n")
                n += 1
        return n

    # ---- listening profile (for suggestions) -----------------------------
    def listening(self) -> list[sqlite3.Row]:
        """Saved songs the user actually played, most played first.

        Plays of other uploads of the same song (skipped as duplicates) count toward the saved copy.
        """
        return self._all(
            "SELECT * FROM (SELECT d.video_id, d.artist, d.title,"
            " CASE WHEN d.song_key IS NULL THEN d.plays"
            "  ELSE (SELECT SUM(t.plays) FROM tracks t WHERE t.song_key = d.song_key) END AS plays,"
            " CASE WHEN d.song_key IS NULL THEN d.last_played"
            "  ELSE (SELECT MAX(t.last_played) FROM tracks t WHERE t.song_key = d.song_key) END AS last_played"
            " FROM tracks d WHERE d.status=?) WHERE plays > 0 ORDER BY plays DESC, last_played DESC", (DONE,))

    def known(self) -> tuple[set[str], set[str]]:
        """(every video id in the catalog, every saved song key): what not to suggest again."""
        with self._lock:
            ids = {r[0] for r in self._db.execute("SELECT video_id FROM tracks")}
            ids |= {r[0] for r in self._db.execute("SELECT video_id FROM suggestions WHERE state<>'new'")}
            keys = {r[0] for r in self._db.execute("SELECT song_key FROM tracks WHERE song_key IS NOT NULL")}
            keys |= {r[0] for r in self._db.execute(
                "SELECT song_key FROM suggestions WHERE state='dismissed' AND song_key IS NOT NULL")}
        return ids, keys

    def replace_suggestions(self, items: list[dict]) -> None:
        """New suggestions replace the unanswered old ones; queued/dismissed ones are remembered."""
        now = time.time()
        with self._lock, self._db:
            self._db.execute("DELETE FROM suggestions WHERE state='new'")
            self._db.executemany(
                "INSERT OR IGNORE INTO suggestions(video_id, song_key, title, artist, album, duration, reason, score,"
                " created) VALUES(?,?,?,?,?,?,?,?,?)",
                [(i["video_id"], song_key(i["artist"], i["title"]), i["title"], i["artist"], i.get("album") or "",
                  i.get("duration"), i.get("reason") or "", i.get("score") or 0, now) for i in items])

    def suggestions(self, state: str = "new") -> list[sqlite3.Row]:
        return self._all("SELECT * FROM suggestions WHERE state=? ORDER BY score DESC", (state,))

    def mark_previewed(self, video_ids: list[str]) -> None:
        """About to open these in the browser: note them as skipped first, so the play isn't downloaded."""
        now = time.time()
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO tracks(video_id, status, source, music_hint, seen_title, first_seen, plays,"
                " note) VALUES(?,?,'suggested',1,?,?,0,?)",
                [(v, SKIPPED, "", now, PREVIEWED) for v in video_ids])

    def answer_suggestions(self, video_ids: list[str], state: str) -> None:
        """state: 'queued' (also adds them to the download queue) or 'dismissed'."""
        with self._lock, self._db:
            self._db.executemany("UPDATE suggestions SET state=? WHERE video_id=?", [(state, v) for v in video_ids])
            if state == "queued":
                rows = self._db.execute(f"SELECT video_id, artist, title FROM suggestions WHERE video_id IN"
                                        f" ({','.join('?' * len(video_ids))})", video_ids).fetchall()
        if state == "queued":
            for r in rows:
                self.add_sighting(r[0], source="suggested", title=f"{r[1]} - {r[2]}", priority=PRIORITY_COLLECTION,
                                  music_hint=True, played=False)
            with self._lock, self._db:  # previewed earlier: now wanted after all
                self._db.executemany(
                    "UPDATE tracks SET status=?, note=NULL, next_try=0, priority=? WHERE video_id=? AND note=?",
                    [(QUEUED, PRIORITY_COLLECTION, v, PREVIEWED) for v in video_ids])

    # ---- followed artists ------------------------------------------------
    def follows(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM follows ORDER BY name COLLATE NOCASE")

    def follow(self, artist_id: str, name: str, seen: list[str]) -> None:
        import json

        self._run("INSERT INTO follows(artist_id, name, added, last_checked, seen) VALUES(?,?,?,?,?)"
                  " ON CONFLICT(artist_id) DO UPDATE SET name=excluded.name",
                  (artist_id, name, time.time(), time.time(), json.dumps(sorted(seen))))

    def unfollow(self, artist_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("DELETE FROM follows WHERE artist_id=?", [(a,) for a in artist_ids])

    def followed_checked(self, artist_id: str, seen: list[str]) -> None:
        import json

        self._run("UPDATE follows SET last_checked=?, seen=? WHERE artist_id=?",
                  (time.time(), json.dumps(sorted(seen)), artist_id))
