from songsnag import tidy
from songsnag.catalog import Catalog


def test_merges_spellings_and_keeps_catalog_paths(tmp_path):
    music = tmp_path / "music"
    a, b, c = music / "＂Weird Al＂ Yankovic", music / "Weird Al Yankovic", music / "Other"
    for d in (a, b, c):
        d.mkdir(parents=True)
    (a / "Fat.mp3").write_bytes(b"1")
    (a / "Eat It.mp3").write_bytes(b"2")
    (b / "Fat.mp3").write_bytes(b"3")  # same name in both: kept, renamed
    (c / "x.mp3").write_bytes(b"4")
    (a / "my own rip.flac").write_bytes(b"5")  # not SongSnag's: must stay put
    cat = Catalog(tmp_path / "c.db")
    for vid, path in (("eatit000001", a / "Eat It.mp3"), ("fat00000001", a / "Fat.mp3")):
        cat.add_sighting(vid, source="brave")
        cat.next_job()
        cat.finish_done(vid, {"artist": "Weird Al", "title": path.stem, "path": str(path)})

    plan = tidy.split_artists(music)
    assert [(k.name, [o.name for o in others]) for k, others in plan] == [
        ("Weird Al Yankovic", ["＂Weird Al＂ Yankovic"])]  # the plain ASCII spelling wins
    assert tidy.untracked(cat, plan) == 1
    assert tidy.merge(cat, plan) == 2
    assert sorted(p.name for p in b.iterdir()) == ["Eat It.mp3", "Fat (＂Weird Al＂ Yankovic).mp3", "Fat.mp3"]
    assert [p.name for p in a.iterdir()] == ["my own rip.flac"]
    assert cat.get("eatit000001")["path"] == str(b / "Eat It.mp3")
    assert cat.get("fat00000001")["path"] == str(b / "Fat (＂Weird Al＂ Yankovic).mp3")
