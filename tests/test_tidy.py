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
    cat = Catalog(tmp_path / "c.db")
    cat.add_sighting("eatit000001", source="brave")
    cat.next_job()
    cat.finish_done("eatit000001", {"artist": "Weird Al", "title": "Eat It", "path": str(a / "Eat It.mp3")})

    plan = tidy.split_artists(music)
    assert [(k.name, [o.name for o in others]) for k, others in plan] == [
        ("Weird Al Yankovic", ["＂Weird Al＂ Yankovic"])]  # the plain ASCII spelling wins
    assert tidy.merge(cat, plan) == 2
    assert not a.exists() and sorted(p.name for p in b.iterdir()) == ["Eat It.mp3", "Fat (＂Weird Al＂ Yankovic).mp3",
                                                                       "Fat.mp3"]
    assert cat.get("eatit000001")["path"] == str(b / "Eat It.mp3")
    assert tidy.split_artists(music) == []
