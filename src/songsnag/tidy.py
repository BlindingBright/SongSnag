"""Merging artist folders that are the same artist spelled differently.

Older versions (and other tools) could file one artist under several names:
＂Weird Al＂ Yankovic, 'Weird' Al Yankovic, Weird Al Yankovic. New songs already
go into the existing folder; this tidies up the ones already split.
"""

from __future__ import annotations

import logging
import shutil
from collections import defaultdict
from pathlib import Path

from . import youtube
from .catalog import Catalog
from .downloader import AUDIO_EXTS, folder_name

log = logging.getLogger(__name__)


def split_artists(music: Path) -> list[tuple[Path, list[Path]]]:
    """[(folder to keep, folders to merge into it)] for every artist filed under more than one name."""
    groups: dict[str, list[Path]] = defaultdict(list)
    try:
        for d in music.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                groups[youtube.loose(d.name)].append(d)
    except OSError:
        return []
    out = []
    for key, dirs in groups.items():
        if len(dirs) < 2 or not key:
            continue

        def score(d: Path):
            clean = folder_name(d.name) == d.name and d.name.isascii()  # no full-width quotes
            return (clean, sum(1 for p in d.iterdir() if p.suffix.lower() in AUDIO_EXTS), d.name)

        dirs.sort(key=score, reverse=True)
        out.append((dirs[0], dirs[1:]))
    return out


def _tracked(catalog: Catalog) -> dict[str, str]:
    return {str(Path(r["path"])): r["video_id"] for r in catalog.search(status="done", limit=10**7) if r["path"]}


def untracked(catalog: Catalog, plan: list[tuple[Path, list[Path]]]) -> int:
    """Files in the folders to merge that SongSnag didn't save; merge() leaves them where they are."""
    known = _tracked(catalog)
    return sum(1 for _, others in plan for d in others for p in d.rglob("*") if p.is_file() and str(p) not in known)


def merge(catalog: Catalog, plan: list[tuple[Path, list[Path]]]) -> int:
    """Move SongSnag's songs, keep the catalog pointing at them, and remove the emptied folders.

    Only files in the catalog move: the music folder may hold the user's own rips, which other players know by path.
    Returns how many files moved.
    """
    by_path = _tracked(catalog)
    moved = 0
    for keep, others in plan:
        for d in others:
            for src in sorted(d.rglob("*")):
                if not src.is_file() or str(src) not in by_path:
                    continue
                dst = keep / src.relative_to(d)
                if dst.exists():
                    dst = dst.with_name(f"{dst.stem} ({d.name}){dst.suffix}")
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(src, dst)
                except OSError as e:
                    log.warning("could not move %s: %s", src, e)
                    continue
                moved += 1
                catalog.set_path(by_path[str(src)], str(dst))
            for sub in sorted((p for p in d.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    sub.rmdir()
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                log.info("left %s in place: it still has files SongSnag didn't save", d)
    return moved
