"""Cross-process graph-change notifications.

The incremental watcher (``cviz watch``), Git-hook refreshes (``cviz update``)
and the graph web server usually run in different processes. They share a small
append-only JSON-lines log: writers append one event per line, the web server
tails the file and relays each event to the browser over Server-Sent Events
(``GET /api/events``).

A file was preferred over a socket because it needs no extra daemon, survives
either side restarting, and works the same on macOS, Linux and Windows. The log
is trimmed to its last :data:`KEEP_LINES` events once it grows past
:data:`MAX_BYTES`, so it never grows without bound.

Event shape::

    {"type": "graph:changed", "files": ["/abs/a.py", ...], "repo": "/abs/repo",
     "ts": 1730985600.123, "pid": 4242}

Types: ``graph:changed`` (incremental update of some files) and
``index:done`` (a repository was fully re-indexed or re-synchronised).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

GRAPH_CHANGED = "graph:changed"
INDEX_DONE = "index:done"

MAX_BYTES = 1_000_000
KEEP_LINES = 500
# Cap the file list carried by one event; the page only needs to know whether
# a displayed file is among them, and "repo" covers the overflow case.
MAX_FILES_PER_EVENT = 500

_write_lock = threading.Lock()
_listeners: List[Callable[[Dict[str, Any]], None]] = []


def events_file() -> Path:
    """Location of the shared event log (``CVIZ_EVENTS_FILE`` overrides it)."""
    override = os.getenv("CVIZ_EVENTS_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".code-visualizer" / "events.jsonl"


def add_listener(callback: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
    """Register an in-process listener; returns a function that removes it."""
    _listeners.append(callback)

    def _remove() -> None:
        try:
            _listeners.remove(callback)
        except ValueError:
            pass

    return _remove


def _trim(path: Path) -> None:
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP_LINES:]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def publish(event_type: str, files: Iterable[str] = (), repo: Optional[str] = None,
            **extra: Any) -> Dict[str, Any]:
    """Append an event to the shared log and notify in-process listeners.

    Never raises: a failure to notify must not break indexing.
    """
    file_list = sorted({str(f).replace("\\", "/") for f in files if f})
    event: Dict[str, Any] = {
        "type": event_type,
        "files": file_list[:MAX_FILES_PER_EVENT],
        "files_truncated": len(file_list) > MAX_FILES_PER_EVENT,
        "repo": str(repo).replace("\\", "/") if repo else None,
        "ts": time.time(),
        "pid": os.getpid(),
    }
    event.update(extra)
    if os.getenv("CVIZ_EVENTS_DISABLED", "").lower() not in ("1", "true", "yes"):
        path = events_file()
        try:
            with _write_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                _trim(path)
        except (OSError, ValueError):  # unwritable / invalid path: best effort
            pass
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:  # noqa: BLE001 - listeners are best-effort
            pass
    return event


class EventTail:
    """Incrementally read events appended to the log since the last call.

    Starts at the current end of the file (only *new* events are delivered).
    When the log is trimmed (replaced by a shorter file with a new inode) or
    truncated, it is re-read from the start and only events newer than the
    last one delivered are returned, so the event that triggered the trim is
    not lost and nothing is delivered twice.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or events_file()
        st = self._stat()
        self._offset = st.st_size if st else 0
        self._ident = (st.st_dev, st.st_ino) if st else None
        self._partial = ""
        # Events written before this tail existed are never delivered.
        self._last_ts = time.time()

    def _stat(self) -> Optional[os.stat_result]:
        try:
            return self.path.stat()
        except OSError:
            return None

    def poll(self) -> List[Dict[str, Any]]:
        st = self._stat()
        if st is None:
            self._offset, self._ident, self._partial = 0, None, ""
            return []
        ident = (st.st_dev, st.st_ino)
        replaced = ident != self._ident or st.st_size < self._offset
        if replaced:
            self._ident, self._offset, self._partial = ident, 0, ""
        if st.st_size == self._offset:
            return []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            return []
        lines = (self._partial + chunk).split("\n")
        self._partial = lines.pop()  # incomplete trailing line, if any
        events: List[Dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = float(event.get("ts") or 0)
            if replaced and ts <= self._last_ts:
                continue
            events.append(event)
        if events:
            self._last_ts = max(self._last_ts, max(float(e.get("ts") or 0) for e in events))
        return events
