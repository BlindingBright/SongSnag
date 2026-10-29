import time

from songsnag.catalog import DONE, FAILED, PRIORITY_BACKFILL, PRIORITY_LIVE, QUEUED, SKIPPED, Catalog


def cat(tmp_path):
    return Catalog(tmp_path / "c.db")


def test_add_and_dedupe(tmp_path):
    c = cat(tmp_path)
    assert c.add_sighting("aaaaaaaaaaa", source="brave", title="A", played_at=100)
    assert not c.add_sighting("aaaaaaaaaaa", source="brave", title="A", played_at=200)
    assert not c.add_sighting("aaaaaaaaaaa", source="brave-history", played_at=100)  # replayed import: no new play
    r = c.get("aaaaaaaaaaa")
    assert r["plays"] == 2 and r["status"] == QUEUED


def test_live_jobs_jump_the_queue(tmp_path):
    c = cat(tmp_path)
    c.add_sighting("backfill001", source="takeout", priority=PRIORITY_BACKFILL)
    c.add_sighting("live0000001", source="brave", priority=PRIORITY_LIVE)
    assert c.next_job()["video_id"] == "live0000001"
    assert c.next_job()["video_id"] == "backfill001"
    assert c.next_job() is None


def test_backfill_item_played_live_gets_bumped(tmp_path):
    c = cat(tmp_path)
    c.add_sighting("old00000001", source="takeout", priority=PRIORITY_BACKFILL)
    c.add_sighting("old00000002", source="takeout", priority=PRIORITY_BACKFILL)
    c.add_sighting("old00000002", source="brave", priority=PRIORITY_LIVE)
    assert c.next_job()["video_id"] == "old00000002"


def test_working_jobs_recover_after_crash(tmp_path):
    c = cat(tmp_path)
    c.add_sighting("aaaaaaaaaaa", source="brave")
    assert c.next_job()
    c.close()
    c = cat(tmp_path)
    assert c.get("aaaaaaaaaaa")["status"] == "working"  # the tray opening the DB must not steal the job
    c.close()
    c = Catalog(tmp_path / "c.db", recover=True)  # only the worker recovers
    assert c.get("aaaaaaaaaaa")["status"] == QUEUED


def test_finish_states_and_retry(tmp_path):
    c = cat(tmp_path)
    for v in ("done0000000", "skip0000000", "fail0000000", "retr0000000"):
        c.add_sighting(v, source="brave")
        c.next_job()
    c.finish_done("done0000000", {"artist": "A", "title": "T", "path": str(tmp_path / "a.opus")})
    c.finish_skipped("skip0000000", "not music")
    c.finish_failed("fail0000000", "gone", retry_in=None)
    c.finish_failed("retr0000000", "net", retry_in=60)
    n = c.counts()
    assert (n[DONE], n[SKIPPED], n[FAILED], n[QUEUED]) == (1, 1, 1, 1)
    assert c.next_job() is None  # the retry is scheduled in the future
    assert c.next_wakeup() > time.time()
    assert c.retry_failed() == 1
    c.requeue(["skip0000000"], force=True)
    r = c.get("skip0000000")
    assert r["status"] == QUEUED and r["force"] == 1
    assert [r["video_id"] for r in c.search("A")] == ["done0000000"]


def test_exports(tmp_path):
    c = cat(tmp_path)
    song = tmp_path / "music" / "A" / "T.opus"
    song.parent.mkdir(parents=True)
    song.write_bytes(b"x")
    c.add_sighting("aaaaaaaaaaa", source="brave")
    c.next_job()
    c.finish_done("aaaaaaaaaaa", {"artist": "A", "title": "T", "duration": 200, "path": str(song)})
    assert c.export_m3u(tmp_path / "music" / "all.m3u8") == 1
    assert "A/T.opus" in (tmp_path / "music" / "all.m3u8").read_text()
    assert c.export_csv(tmp_path / "all.csv") == 1
    assert c.done_under(tmp_path / "music")[0]["video_id"] == "aaaaaaaaaaa"


def test_album_checked_migration_and_queue(tmp_path):
    import sqlite3

    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE tracks (video_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'queued',"
                " priority INTEGER NOT NULL DEFAULT 0, source TEXT, music_hint INTEGER NOT NULL DEFAULT 0,"
                " force INTEGER NOT NULL DEFAULT 0, seen_title TEXT, first_seen REAL, last_played REAL,"
                " plays INTEGER NOT NULL DEFAULT 0, title TEXT, artist TEXT, album TEXT, channel TEXT,"
                " duration REAL, path TEXT, codec TEXT, abr REAL, downloaded_at REAL,"
                " attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, note TEXT)")
    con.execute("INSERT INTO tracks(video_id, status, path) VALUES('aaaaaaaaaaa', 'done', '/x.mp3')")
    con.commit()
    con.close()
    c = Catalog(old)  # a 1.0 catalog gains the new column
    assert [r["video_id"] for r in c.needs_album()] == ["aaaaaaaaaaa"]
    c.set_album("aaaaaaaaaaa", None)  # looked up, nothing found: not asked again...
    assert c.needs_album() == []
    assert len(c.needs_album(["aaaaaaaaaaa"])) == 1  # ...unless the user asks for that song
    c.set_album("aaaaaaaaaaa", "LP")
    assert c.get("aaaaaaaaaaa")["album"] == "LP"


def test_song_key_finds_the_same_song_from_another_upload(tmp_path):
    from songsnag.albums import song_key

    assert song_key('"Weird Al" Yankovic', "Fat") == song_key("'Weird' Al Yankovic", "Fat (Remastered)")
    assert song_key("A feat. B", "Song") == song_key("A", "Song")
    assert song_key("A", "Song") != song_key("A", "Song (Live)")

    c = cat(tmp_path)
    song = tmp_path / "Fat.mp3"
    song.write_bytes(b"x")
    c.add_sighting("video000001", source="brave")
    c.next_job()
    c.finish_done("video000001", {"artist": '"Weird Al" Yankovic', "title": "Fat", "path": str(song)})
    key = song_key("Weird Al Yankovic", "Fat")
    assert c.saved_copy(key, exclude="topic000001")["video_id"] == "video000001"
    assert c.saved_copy(key, exclude="video000001") is None  # a re-download of the song itself
    song.unlink()
    assert c.saved_copy(key, exclude="topic000001") is None  # the file is gone: download it again


def test_song_key_backfilled_for_old_catalogs(tmp_path):
    c = cat(tmp_path)
    c.add_sighting("aaaaaaaaaaa", source="brave")
    c.next_job()
    c.finish_done("aaaaaaaaaaa", {"artist": "A", "title": "T"})
    c._run("DROP INDEX tracks_song")
    c._run("ALTER TABLE tracks DROP COLUMN song_key")  # as a 1.0 catalog was
    c.close()
    c = cat(tmp_path)
    assert c.get("aaaaaaaaaaa")["song_key"] == "a|t"


def test_unplayed_requests_do_not_count_as_plays(tmp_path):
    c = cat(tmp_path)
    assert c.add_sighting("album000001", source="album", played=False)
    assert not c.add_sighting("album000001", source="album", played=False)
    r = c.get("album000001")
    assert r["plays"] == 0 and r["last_played"] is None
    c.add_sighting("album000001", source="brave")  # then actually played
    assert c.get("album000001")["plays"] == 1


def test_version_changes_on_any_write(tmp_path):
    c = cat(tmp_path)
    other = cat(tmp_path)
    v = c.version()
    assert c.version() == v
    c.add_sighting("aaaaaaaaaaa", source="brave")
    assert c.version() != v
    v = c.version()
    other.add_sighting("bbbbbbbbbbb", source="brave")  # the worker's connection
    assert c.version() != v


def test_suggestions_lifecycle(tmp_path):
    c = cat(tmp_path)
    c.replace_suggestions([{"video_id": "sugg0000001", "artist": "A", "title": "One", "score": 2},
                           {"video_id": "sugg0000002", "artist": "B", "title": "Two", "score": 1}])
    assert [r["video_id"] for r in c.suggestions()] == ["sugg0000001", "sugg0000002"]
    c.answer_suggestions(["sugg0000001"], "queued")
    c.answer_suggestions(["sugg0000002"], "dismissed")
    assert c.get("sugg0000001")["status"] == QUEUED and c.get("sugg0000001")["plays"] == 0
    ids, keys = c.known()
    assert {"sugg0000001", "sugg0000002"} <= ids and "b|two" in keys
    c.replace_suggestions([])  # answered ones survive a refresh
    assert c.suggestions() == [] and len(c.suggestions("dismissed")) == 1


def test_follows(tmp_path):
    import json

    c = cat(tmp_path)
    c.follow("UCartist", "Band", ["MPREb_1"])
    c.followed_checked("UCartist", ["MPREb_1", "MPREb_2"])
    (f,) = c.follows()
    assert f["name"] == "Band" and json.loads(f["seen"]) == ["MPREb_1", "MPREb_2"]
    c.unfollow(["UCartist"])
    assert c.follows() == []


def test_song_key_ignores_notes_but_not_versions():
    from songsnag.albums import song_key

    plain = song_key('"Weird Al" Yankovic', "White & Nerdy")
    assert song_key("Weird Al Yankovic", 'White & Nerdy (Parody of "Ridin\'" by X feat. Y) (feat. Y)') == plain
    assert song_key("A", "Song [Official Video]") == song_key("A", "Song")
    assert song_key("A", "Song (Live at Wembley)") != song_key("A", "Song")
    assert song_key("A", "Song (Tape B Remix)") != song_key("A", "Song")
    assert song_key("A", "(I Can't Get No) Satisfaction") == "a|satisfaction"


def test_previewing_a_suggestion_does_not_download_it(tmp_path):
    c = cat(tmp_path)
    c.replace_suggestions([{"video_id": "sugg0000001", "artist": "A", "title": "One"}])
    c.mark_previewed(["sugg0000001"])
    assert not c.add_sighting("sugg0000001", source="brave", priority=PRIORITY_LIVE)  # the browser play
    assert c.get("sugg0000001")["status"] == SKIPPED and c.next_job() is None
    c.answer_suggestions(["sugg0000001"], "queued")  # liked it after all
    assert c.next_job()["video_id"] == "sugg0000001"


def test_plays_of_duplicate_uploads_count_for_the_saved_song(tmp_path):
    from songsnag.albums import song_key

    c = cat(tmp_path)
    c.add_sighting("topic000001", source="album", played=False)
    c.next_job()
    c.finish_done("topic000001", {"artist": "A", "title": "Song"})
    for t in (100, 200, 300):
        c.add_sighting("video000001", source="brave", played_at=t)
    c.next_job()
    c.finish_skipped("video000001", "already saved", {"song_key": song_key("A", "Song")})
    (r,) = c.listening()
    assert r["video_id"] == "topic000001" and r["plays"] == 3 and r["last_played"] == 300
