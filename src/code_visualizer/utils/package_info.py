"""Installed-distribution metadata.

Distributed as ``code-visualizer`` (import package ``code_visualizer``); both
spellings of the distribution name are tried, as installers record either.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Optional

DISTRIBUTION_NAMES = ("code-visualizer", "code_visualizer")


def installed_version() -> Optional[str]:
    """Version of the installed distribution, or None when not installed."""
    for name in DISTRIBUTION_NAMES:
        try:
            return version(name)
        except PackageNotFoundError:
            continue
    return None
