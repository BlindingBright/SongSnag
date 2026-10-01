"""Keeping yt-dlp current without reinstalling the app.

YouTube changes often and old yt-dlp versions stop working, so SongSnag can
fetch the newest yt-dlp (and the yt-dlp-ejs version it pins) from PyPI,
verify their sha256, and unpack them into the data dir. Each download worker
calls `activate()` at start, which puts that folder ahead of the bundled copy,
but only if it is newer.

Nothing here may import yt_dlp: activate() has to run first.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import logging
import re
import shutil
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

from . import __version__, paths

log = logging.getLogger(__name__)
PYPI = "https://pypi.org/pypi/{name}/json"
PYPI_VERSION = "https://pypi.org/pypi/{name}/{version}/json"


def updates_dir() -> Path:
    return paths.data_dir() / "ytdlp"


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def bundled_version() -> str | None:
    spec = importlib.util.find_spec("yt_dlp")
    if not spec or not spec.origin:
        return None
    try:
        text = (Path(spec.origin).parent / "version.py").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)", text, re.M)
    return m.group(1) if m else None


def _installed_update() -> tuple[str, Path] | None:
    base = updates_dir()
    try:
        version = (base / "current").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    folder = base / version
    return (version, folder) if (folder / "yt_dlp").is_dir() else None


def activate() -> str | None:
    """Prefer a downloaded yt-dlp when it is newer than the bundled one. Returns its version."""
    upd = _installed_update()
    if not upd:
        return None
    version, folder = upd
    bundled = bundled_version()
    if bundled and _vtuple(bundled) >= _vtuple(version):
        return None
    sys.path.insert(0, str(folder))
    return version


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"SongSnag/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        return r.read()


def _wheel(meta: dict) -> tuple[str, str]:
    for f in meta["urls"]:
        if f["packagetype"] == "bdist_wheel" and f["filename"].endswith("-py3-none-any.whl"):
            return f["url"], f["digests"]["sha256"]
    raise RuntimeError(f"no universal wheel for {meta['info']['name']} {meta['info']['version']}")


def _fetch_verified(url: str, sha256: str) -> bytes:
    data = _get(url, timeout=120)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(f"checksum mismatch for {url}")
    return data


def active_version() -> str | None:
    upd = _installed_update()
    bundled = bundled_version()
    if upd and (not bundled or _vtuple(upd[0]) > _vtuple(bundled)):
        return upd[0]
    return bundled


def check_and_install() -> str | None:
    """Install a newer yt-dlp if PyPI has one. Returns the new version, or None if already current."""
    meta = json.loads(_get(PYPI.format(name="yt-dlp")))
    latest = meta["info"]["version"]
    current = active_version()
    if current and _vtuple(current) >= _vtuple(latest):
        return None

    wheels = [_wheel(meta)]
    for req in meta["info"].get("requires_dist") or []:
        m = re.match(r"yt-dlp-ejs\s*==\s*([\w.]+)", req)
        if m:
            ejs = json.loads(_get(PYPI_VERSION.format(name="yt-dlp-ejs", version=m.group(1))))
            wheels.append(_wheel(ejs))
            break

    base = updates_dir()
    staging = base / f"{latest}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for url, digest in wheels:
        with zipfile.ZipFile(io.BytesIO(_fetch_verified(url, digest))) as z:
            for name in z.namelist():
                if ".dist-info/" in name:
                    continue
                target = (staging / name).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise RuntimeError(f"unsafe path in wheel: {name}")
            z.extractall(staging, [n for n in z.namelist() if ".dist-info/" not in n])

    final = base / latest
    shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)
    previous = _installed_update()
    (base / "current").write_text(latest, encoding="utf-8")
    # Keep the version a running download worker may still be using; drop anything older.
    keep = {latest, previous[0] if previous else None}
    for old in base.iterdir():
        if old.is_dir() and old.name not in keep:
            shutil.rmtree(old, ignore_errors=True)
    log.info("installed yt-dlp %s (was %s)", latest, current)
    return latest
