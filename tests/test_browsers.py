import json
import time

from conftest import add_visits, make_history

from songsnag import browsers

NOW = time.time()


def test_find_profiles(tmp_path):
    root = tmp_path / "Brave-Browser"
    make_history(root / "Default" / "History", [])
    make_history(root / "Profile 2" / "History", [])
    (root / "Profile 3").mkdir()  # no History: ignored
    (root / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Profile 2": {"name": "Work"}}}}))
    profiles = browsers.find_profiles([browsers.Root("brave", "Brave-Browser", root)])
    assert [(p.key, p.name) for p in profiles] == [("Brave-Browser/Default", "Default"),
                                                   ("Brave-Browser/Profile 2", "Work")]


def test_read_visits_filters_and_cursor(tmp_path):
    h = make_history(tmp_path / "History", [
        ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "Song A - YouTube", NOW - 100),
        ("https://example.com/", "Other", NOW - 90),
        ("https://www.youtube.com/shorts/bbbbbbbbbbb", "Short", NOW - 80),
        ("https://music.youtube.com/watch?v=ccccccccccc&list=x", "Song C - YouTube Music", NOW - 70),
    ])
    visits, newest = browsers.read_visits(h)
    assert [(v.video_id, v.title) for v in visits] == [("aaaaaaaaaaa", "Song A"), ("ccccccccccc", "Song C")]
    assert newest == 4
    assert abs(visits[0].time - (NOW - 100)) < 1

    add_visits(h, [("https://youtu.be/ddddddddddd", "D", NOW)])
    visits, newest = browsers.read_visits(h, after_id=4)
    assert [v.video_id for v in visits] == ["ddddddddddd"] and newest == 5


def test_read_visits_since(tmp_path):
    h = make_history(tmp_path / "History", [
        ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "old", NOW - 40 * 86400),
        ("https://www.youtube.com/watch?v=bbbbbbbbbbb", "new", NOW - 86400),
    ])
    visits, _ = browsers.read_visits(h, since=NOW - 30 * 86400)
    assert [v.video_id for v in visits] == ["bbbbbbbbbbb"]


def test_unreadable_history_raises(tmp_path):
    bad = tmp_path / "History"
    bad.write_bytes(b"not a database" * 100)
    try:
        browsers.read_visits(bad)
    except browsers.HistoryReadError:
        pass
    else:
        raise AssertionError("expected HistoryReadError")


def test_all_three_browsers_found(tmp_path, monkeypatch):
    monkeypatch.setattr(browsers.sys, "platform", "linux")
    monkeypatch.setattr(browsers.Path, "home", classmethod(lambda cls: tmp_path))  # HOME is ignored on Windows
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    cfg = tmp_path / ".config"
    make_history(cfg / "BraveSoftware/Brave-Browser/Default/History", [])
    make_history(cfg / "google-chrome/Default/History", [])
    make_history(cfg / "google-chrome/Profile 1/History", [])
    make_history(tmp_path / ".var/app/com.microsoft.Edge/config/microsoft-edge/Default/History", [])
    found = browsers.find_profiles()
    assert [(p.key, p.browser) for p in found] == [
        ("Brave-Browser/Default", "brave"),  # unchanged from the Brave-only days: cursors keep working
        ("Chrome/Default", "chrome"), ("Chrome/Profile 1", "chrome"), ("Edge-Flatpak/Default", "edge")]
    assert found[1].display == "Chrome: Default"
    assert found[3].cookie_source[0] == "edge"
    assert browsers.browsers_label(found) == "Brave, Chrome and Edge"
    assert browsers.browsers_label(found[:2]) == "Brave and Chrome"

    # Login: the chosen profile wins; otherwise the first watched one.
    assert browsers.login_profile("Chrome/Profile 1", []).key == "Chrome/Profile 1"
    assert browsers.login_profile("", ["Edge-Flatpak/Default"]).key == "Edge-Flatpak/Default"
    assert browsers.login_profile("gone/Default", []).key == "Brave-Browser/Default"
    assert [p.key for p in browsers.selected_profiles(["Chrome/Default"])] == ["Chrome/Default"]
