import sys

from songsnag import tools


def test_frozen_app_finds_bundled_tools(tmp_path, monkeypatch):
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    (tmp_path / "_internal" / "tools").mkdir(parents=True)
    (tmp_path / "_internal" / "tools" / exe).write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert tools.find_ffmpeg() == str(tmp_path / "_internal" / "tools" / exe)


def test_source_install_uses_path(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert tools.find_ffmpeg() == "/usr/bin/ffmpeg"
