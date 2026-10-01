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

QUEUED, WORKING, DONE, SKIPPED, FAILED = "queued", "working", "done", "skipped", "failed"
STATUSES = (QUEUED, WORKING, DONE, SKIPPED, FAILED)

PRIORITY_LIVE = 10  # played just now
PRIORITY_BACKFILL = 0  # imported from history

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

    # ---- state -----------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        rows = self._all("SELECT value FROM state WHERE key=?", (key,))
        return rows[0][0] if rows else default

    def set_state(self, key: str, value: str) -> None:
        self._run("INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))

    # ---- queue -----------------------------------------------------------
    def add_sighting(self, video_id: str, *, source: str, title: str = "", played_at: float | None = None,
                     priority: int = PRIORITY_BACKFILL, music_hint: bool = False) -> bool:
        """Record that a video was played/seen. Returns True if it was newly queued."""
        now = time.time()
        played_at = played_at or now
        with self._lock, self._db:
            row = self._db.execute("SELECT status, last_played FROM tracks WHERE video_id=?", (video_id,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO tracks(video_id, status, priority, source, music_hint, seen_title, first_seen,"
                    " last_played, plays) VALUES(?,?,?,?,?,?,?,?,1)",
                    (video_id, QUEUED, priority, source, int(music_hint), title, now, played_at))
                return True
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

    def pending_count(self) -> int:
        return self._all("SELECT COUNT(*) FROM tracks WHERE status IN (?, ?)", (QUEUED, WORKING))[0][0]

    def runnable_count(self) -> int:
        return self._all("SELECT COUNT(*) FROM tracks WHERE status=? AND next_try<=?", (QUEUED, time.time()))[0][0]

    def next_wakeup(self) -> float | None:
        rows = self._all("SELECT MIN(next_try) FROM tracks WHERE status=?", (QUEUED,))
        return rows[0][0]

    def _meta_sql(self, meta: dict) -> tuple[str, list]:
        cols = [k for k in ("title", "artist", "album", "channel", "duration", "path", "codec", "abr",
                            "album_checked") if k in meta]
        return "".join(f", {c}=?" for c in cols), [meta[c] for c in cols]

    def finish_done(self, video_id: str, meta: dict) -> None:
        extra, args = self._meta_sql(meta)
        self._run(f"UPDATE tracks SET status=?, downloaded_at=?, note=NULL, force=0{extra} WHERE video_id=?",
                  (DONE, time.time(), *args, video_id))

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

    def search(self, text: str = "", status: str | None = None, limit: int = 5000) -> list[sqlite3.Row]:
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
        return self._all(sql, (*args, limit))

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

    def set_album(self, video_id: str, album: str | None) -> None:
        if album:
            self._run("UPDATE tracks SET album=?, album_checked=? WHERE video_id=?", (album, time.time(), video_id))
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
        rows = self._all("SELECT * FROM tracks WHERE status=? AND path IS NOT NULL"
                         " ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE", (DONE,))
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
