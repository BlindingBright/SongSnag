"""User settings, stored as JSON in the config dir."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

AUDIO_FORMATS = {
    "mp3": "MP3 320 kbps (plays everywhere: cars, phones, MP3 players)",
    "best": "Original YouTube stream, no re-encode (Opus/AAC; smaller, not all players)",
    "m4a": "M4A / AAC",
    "opus": "Opus",
}
# Short names for the Library's "Save as" dropdown.
AUDIO_FORMATS_SHORT = {"mp3": "MP3 (320 kbps)", "best": "Original stream", "m4a": "M4A / AAC", "opus": "Opus"}
LAYOUTS = {
    "artist": "Artist folders  (Artist/Title)",
    "flat": "One folder  (Artist - Title)",
}


@dataclass
class Settings:
    music_dir: str = ""
    layout: str = "artist"
    audio_format: str = "mp3"
    music_only: bool = True
    max_minutes: int = 20
    use_cookies: bool = True
    profiles: list[str] = field(default_factory=list)  # browser profiles to watch; empty = all of them
    login_profile: str = ""  # whose YouTube login to use; empty = the first watched profile
    watch_enabled: bool = True
    paused: bool = False
    embed_cover: bool = True
    square_cover: bool = True
    lookup_albums: bool = True  # album, year, track number from YouTube Music
    album_art: bool = True  # official album cover instead of the video thumbnail, when found
    notify: bool = True
    autostart: bool = True
    auto_update_ytdlp: bool = True
    poll_seconds: int = 15
    welcomed: bool = False

    @property
    def music_path(self) -> Path:
        return Path(self.music_dir).expanduser() if self.music_dir else paths.default_music_dir()


def settings_file() -> Path:
    return paths.config_dir() / "settings.json"


def load() -> Settings:
    s = Settings()
    try:
        raw = json.loads(settings_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as e:
        log.warning("settings unreadable, using defaults: %s", e)
        raw = {}
    known = {f.name: f for f in fields(Settings)}
    for key, value in raw.items():
        if key in known and isinstance(value, type(getattr(s, key))):
            setattr(s, key, value)
    if s.audio_format not in AUDIO_FORMATS:
        s.audio_format = "mp3"
    if s.layout not in LAYOUTS:
        s.layout = "artist"
    if not s.music_dir:
        s.music_dir = str(paths.default_music_dir())
    return s


def save(s: Settings) -> None:
    target = settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
    os.replace(tmp, target)
