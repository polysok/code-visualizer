"""Run cviz again from cviz, as a Python interpreter or as the frozen binary.

In the PyInstaller binary ``sys.executable`` is cviz itself; ``cviz_entry.py``
makes it accept ``-c`` and ``-m`` like an interpreter. A process meant to
outlive its parent also needs its own copy of the bundle: a one-file binary
started by another reuses the parent's extraction directory, deleted when
the parent exits (lazy imports then fail, e.g. fastembed in a graph server
started by the MCP server).
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List


def cviz_argv(*args: str) -> List[str]:
    """argv running ``cviz <args>`` with this installation."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "code_visualizer.cli.main", *args]


def detached_env() -> Dict[str, str]:
    """Environment for a cviz process that outlives this one."""
    env = dict(os.environ)
    # PyInstaller >= 6.9: the child extracts its own bundle.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env
