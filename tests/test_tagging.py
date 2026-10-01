import shutil
import subprocess

import pytest

from songsnag import tagging
from songsnag.albums import AlbumInfo

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
INFO = AlbumInfo("The LP", "Band", "1999", 3, 12)


def make(tmp_path, ext, *codec):
    out = tmp_path / f"song.{ext}"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", *codec, str(out)], check=True)
    return out


@pytest.fixture
def cover(tmp_path):
    img = tmp_path / "c.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x64", "-frames:v", "1",
                    str(img)], check=True)
    return img.read_bytes()


def test_mp3_gets_id3v23_album_tags(tmp_path, cover):
    from mutagen.id3 import ID3

    f = make(tmp_path, "mp3")
    tagging.write_album(f, INFO, cover)
    tags = ID3(f)
    assert tags.version[:2] == (2, 3)
    assert str(tags["TALB"]) == "The LP" and str(tags["TPE2"]) == "Band"
    assert str(tags["TRCK"]) == "3/12" and "1999" in str(tags.get("TYER") or tags.get("TDRC"))
    assert tags.getall("APIC")[0].data == cover


def test_m4a(tmp_path, cover):
    from mutagen.mp4 import MP4

    f = make(tmp_path, "m4a", "-c:a", "aac")
    tagging.write_album(f, INFO, cover)
    t = MP4(f)
    assert t["\xa9alb"] == ["The LP"] and t["trkn"] == [(3, 12)] and bytes(t["covr"][0]) == cover


def test_opus(tmp_path, cover):
    import mutagen

    f = make(tmp_path, "opus", "-c:a", "libopus")
    tagging.write_album(f, INFO, cover)
    t = mutagen.File(f)
    assert t["ALBUM"] == ["The LP"] and t["TRACKNUMBER"] == ["3"] and t["METADATA_BLOCK_PICTURE"]


def test_unsupported(tmp_path):
    with pytest.raises(ValueError):
        tagging.write_album(tmp_path / "x.wav", INFO)
