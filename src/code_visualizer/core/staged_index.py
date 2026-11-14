# src/code_visualizer/core/staged_index.py
"""Second stage of a staged index, run in the background by ``cviz graph``.

The first stage (tools.indexing.pipeline.run_tree_sitter_index_async with
``first_pass_files``) indexes the files highest in priority, so the page opens
early; this one indexes the rest. Open pages follow it through ``index:progress``
events on the live badge, then ``index:done`` reloads what they show.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..utils.debug_log import error_logger, info_logger
from . import graph_events

INDEX_PROGRESS = "index:progress"
_PROGRESS_INTERVAL_S = 0.5


def _watcher_lock(watcher: Any, repo: Optional[str]):
    """The update lock of the watcher of *repo*, so no file event interleaves.

    A file saved meanwhile is updated once the stage is over, from its new
    content (the stage writes from what it parsed, which may be older).
    """
    handlers = getattr(watcher, "handlers", None) or {}
    handler = None
    for key, candidate in handlers.items():
        if repo and Path(key).resolve().as_posix() == repo:
            handler = candidate
    if handler is None and len(handlers) == 1:
        handler = next(iter(handlers.values()))
    lock = getattr(handler, "_update_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def start_deferred_index(
    deferred: Dict[str, Any],
    watcher: Any = None,
    repo: Optional[str] = None,
    on_done: Optional[Callable[[], None]] = None,
) -> Optional[threading.Thread]:
    """Index the deferred files of *deferred* in a background thread."""
    if not deferred.get("files"):
        return None
    from ..tools.indexing.pipeline import run_deferred_index_async

    repo = Path(repo).resolve().as_posix() if repo else None
    total = len(deferred["files"])
    last = [0.0]

    def progress(done: int, of: int) -> None:
        now = time.monotonic()
        if done >= of or now - last[0] >= _PROGRESS_INTERVAL_S:
            last[0] = now
            graph_events.publish(INDEX_PROGRESS, repo=repo, done=done, total=of, stage="deferred")

    def run() -> None:
        started = time.monotonic()
        try:
            with _watcher_lock(watcher, repo):
                progress(0, total)
                result = asyncio.run(run_deferred_index_async(deferred, progress=progress))
            info_logger(
                f"[STAGED] {result['files']} deferred files indexed in "
                f"{time.monotonic() - started:.0f}s ({result['failed']} failed to parse)"
            )
            graph_events.publish(graph_events.INDEX_DONE, repo=repo, deferred=total)
        except Exception as exc:  # noqa: BLE001 - the next start-up catches up
            error_logger(
                f"[STAGED] Background indexing stopped: {exc}. The files not indexed yet "
                "are picked up the next time the graph server starts."
            )
            graph_events.publish(INDEX_PROGRESS, repo=repo, done=total, total=total,
                                 stage="deferred", error=str(exc))
        finally:
            deferred.clear()
            if on_done is not None:
                try:
                    on_done()
                except Exception as exc:  # noqa: BLE001
                    error_logger(f"[STAGED] after background indexing: {exc}")

    thread = threading.Thread(target=run, name="cviz-deferred-index", daemon=True)
    thread.start()
    return thread
