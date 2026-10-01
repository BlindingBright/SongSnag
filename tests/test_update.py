import sys

from songsnag import ytdlp_update


def test_vtuple_ordering():
    assert ytdlp_update._vtuple("2026.08.19") < ytdlp_update._vtuple("2026.09.01")
    assert ytdlp_update._vtuple("2026.09.01") < ytdlp_update._vtuple("2026.09.01.1")


def test_activate_prefers_newer_download(monkeypatch):
    base = ytdlp_update.updates_dir()
    (base / "9999.1.1" / "yt_dlp").mkdir(parents=True)
    (base / "current").write_text("9999.1.1")
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert ytdlp_update.activate() == "9999.1.1"
    assert sys.path[0] == str(base / "9999.1.1")


def test_activate_ignores_older_download(monkeypatch):
    base = ytdlp_update.updates_dir()
    (base / "2000.1.1" / "yt_dlp").mkdir(parents=True)
    (base / "current").write_text("2000.1.1")
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert ytdlp_update.activate() is None
