"""Is a newer release of Code Visualizer published?

The latest release on GitHub is asked at most once a day; the answer is kept
in ``~/.code-visualizer/update-check.json`` and shared by every cviz of the
machine (graph servers, the CLI, the desktop app's). Without network, or
slow, nothing waits: the check gives up after a few seconds and the last
answer stays in use. A development install (no version) never checks.

``CVIZ_NO_UPDATE_CHECK=1`` turns it off.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

RELEASES_API = "https://api.github.com/repos/polysok/code-visualizer/releases/latest"
RELEASES_PAGE = "https://github.com/polysok/code-visualizer/releases/latest"
MAX_AGE = 24 * 3600


def cache_file() -> Path:
    custom = os.getenv("CVIZ_UPDATE_CHECK_FILE")
    return Path(custom) if custom else Path.home() / ".code-visualizer" / "update-check.json"


def enabled() -> bool:
    return os.getenv("CVIZ_NO_UPDATE_CHECK", "").lower() not in ("1", "true", "yes")


def parse_version(text: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """``v0.9.9`` / ``0.9.9`` -> (0, 9, 9); None for anything else (dev builds)."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _read_cache() -> dict:
    try:
        data = json.loads(cache_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data: dict) -> None:
    path = cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _fetch(timeout: float) -> Optional[dict]:
    req = urllib.request.Request(RELEASES_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "code-visualizer-update-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - offline, rate-limited, blocked: try again later
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not parse_version(tag):
        return None
    return {"latest": tag.lstrip("v"), "url": data.get("html_url") or RELEASES_PAGE}


def latest_release(max_age: float = MAX_AGE, timeout: float = 3.0, refresh: bool = True) -> Optional[dict]:
    """``{"latest": "0.9.9", "url": ...}`` of the last check, refreshed when
    older than *max_age* (unless ``refresh`` is False); None when unknown."""
    cached = _read_cache()
    fresh = time.time() - float(cached.get("checked_at") or 0) < max_age
    if refresh and not fresh:
        found = _fetch(timeout)
        # A failed check is recorded too: the next try waits as long.
        cached = {**(found or {k: cached[k] for k in ("latest", "url") if k in cached}),
                  "checked_at": time.time()}
        _write_cache(cached)
    return {"latest": cached["latest"], "url": cached.get("url") or RELEASES_PAGE} if cached.get("latest") else None


def update_available(current: Optional[str] = None, refresh: bool = True) -> Optional[dict]:
    """``{"current", "latest", "url"}`` when a newer release than *current*
    (default: the installed version) is published, else None."""
    if not enabled():
        return None
    if current is None:
        from .package_info import installed_version

        current = installed_version()
    mine = parse_version(current)
    if mine is None:  # development install: nothing to compare
        return None
    release = latest_release(refresh=refresh)
    if not release or (parse_version(release["latest"]) or (0, 0, 0)) <= mine:
        return None
    return {"current": current, "latest": release["latest"], "url": release["url"]}
