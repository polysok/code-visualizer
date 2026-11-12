"""HTTP endpoints for the standalone dependency-graph page.

Mounted on the visualisation server (``viz/server.py``) ahead of its SPA
fallback. Everything here is read-only against the graph, except
``POST /api/open`` which launches the developer's IDE as a fallback when its
URL scheme (``vscode://``, ``rider://``, ``zed://``) is unavailable.

Access control
--------------
* Requests from the loopback interface are trusted, like the rest of the
  visualisation server (which binds to 127.0.0.1 by default).
* Any other client must present ``CVIZ_API_KEY`` (``Authorization: Bearer``,
  ``X-API-Key`` or, for ``EventSource`` which cannot set headers, an
  ``api_key`` query parameter). Without a configured key, remote clients are
  refused.
* ``POST /api/open`` and ``POST /api/focus`` are loopback-only, require a JSON
  body (so a cross-site "simple" form/text POST cannot trigger them without a
  CORS preflight, which the server rejects) and refuse foreign ``Origin``
  headers.
* File paths are limited to indexed repositories or the sandbox roots
  (``is_path_allowed``).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from ..core import graph_events
from ..utils.path_sandbox import is_path_allowed
from . import neighborhood as nb

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"

_LOCAL_ORIGIN_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")

# ---------------------------------------------------------------------------
# Shared state / helpers
# ---------------------------------------------------------------------------


def _db_manager():
    from . import server  # late import: server imports this module

    if server.db_manager is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    return server.db_manager


def _session():
    return _db_manager().get_driver().session()


def path_mapper() -> nb.PathMapper:
    return nb.PathMapper.from_env()


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def guard(request: Request) -> None:
    """Allow loopback clients; require the API key from anyone else."""
    if _is_loopback(request):
        return
    from ..api.auth import _extract_provided_key, _keys_match, get_configured_api_key

    configured = get_configured_api_key()
    if not configured:
        raise HTTPException(
            status_code=403,
            detail="Remote access to the graph API requires CVIZ_API_KEY to be configured.",
        )
    provided = _extract_provided_key(
        request.headers.get("authorization"), request.headers.get("x-api-key")
    ) or request.query_params.get("api_key")
    if not provided or not _keys_match(provided, configured):
        raise HTTPException(status_code=401, detail="Invalid or missing API key",
                            headers={"WWW-Authenticate": "Bearer"})


def _guard_local_write(request: Request) -> None:
    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail="Only available from this machine (127.0.0.1).")
    origin = request.headers.get("origin")
    if origin and origin != "null" and not _LOCAL_ORIGIN_RE.match(origin):
        raise HTTPException(status_code=403, detail="Foreign origin refused.")
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json.")


_repo_cache: Dict[str, Any] = {"ts": 0.0, "roots": []}


def indexed_repo_roots(max_age: float = 30.0) -> List[str]:
    """Paths of indexed repositories (as stored in the graph), cached briefly."""
    now = time.time()
    if now - _repo_cache["ts"] < max_age:
        return _repo_cache["roots"]
    roots: List[str] = []
    try:
        with _session() as session:
            result = session.run("MATCH (r:Repository) RETURN r.path AS path")
            rows = result.data() if hasattr(result, "data") else [dict(r) for r in result]
        roots = sorted({str(r["path"]).replace("\\", "/").rstrip("/") for r in rows if r.get("path")})
    except Exception:  # noqa: BLE001 - no database yet, or a transient failure
        roots = _repo_cache["roots"]
    _repo_cache.update(ts=now, roots=roots)
    return roots


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def is_indexed_path(path: str) -> bool:
    """True when *path* (indexed form) lies inside an indexed repository."""
    p = str(path).replace("\\", "/")
    try:
        p = Path(p).resolve().as_posix()
    except OSError:
        pass
    return any(_under(p, root) for root in indexed_repo_roots())


def is_readable_path(path: str) -> bool:
    return is_path_allowed(Path(path)) or is_indexed_path(path)


def _parse_kinds(kinds: Optional[str]) -> List[str]:
    if not kinds:
        return ["CALLS", "INHERITS"]
    return [k.strip().upper() for k in kinds.split(",") if k.strip()]


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get("/graph", include_in_schema=False)
def graph_page():
    page = STATIC_DIR / "graph.html"
    if not page.is_file():
        return HTMLResponse("graph.html missing from the package", status_code=500)
    return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/graph-assets/{name}", include_in_schema=False)
def graph_asset(name: str):
    # Flat directory, explicit allow-list of extensions: no traversal possible.
    if "/" in name or "\\" in name or name.startswith(".") or not re.match(r"^[\w.\-]+\.(js|css|svg|woff2)$", name):
        raise HTTPException(status_code=404, detail="Not found")
    asset = STATIC_DIR / name
    if not asset.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    if name.endswith(".woff2"):  # fonts never change for a given name
        return FileResponse(asset, media_type="font/woff2", headers={"Cache-Control": "public, max-age=604800"})
    return FileResponse(asset, headers={"Cache-Control": "no-cache"})


@router.get("/api/graph-config")
def graph_config(request: Request):
    guard(request)
    mapper = path_mapper()
    from ..utils.package_info import installed_version

    return {
        "version": installed_version() or "0.0.0 (dev)",
        "default_ide": os.getenv("CVIZ_DEFAULT_IDE", "vscode"),
        "ides": sorted(IDE_COMMANDS.keys()),
        "path_map": [{"indexed": a, "local": b} for a, b in mapper.pairs],
        "max_depth": nb.MAX_DEPTH,
        "default_max_nodes": nb.DEFAULT_MAX_NODES,
        "repo": _served_repo(mapper),
    }


def _served_repo(mapper) -> Optional[Dict[str, Any]]:
    """The repository whose database this server serves (one per repository)."""
    root = _index_services.get("repo_root")
    if not root:
        return None
    watcher = _index_services.get("watcher")
    watched = bool(watcher) and any(
        Path(p).resolve().as_posix() == Path(root).resolve().as_posix() for p in watcher.handlers
    )
    local = mapper.to_local(root)
    from ..core import companions

    try:
        described = companions.describe(root)
    except Exception:  # noqa: BLE001 - unreadable list: shown as none
        described = {"sources": [], "pending": False}
    return {
        "name": Path(local).name, "path": local, "watched": watched,
        # Companion sources, for the count next to the name and for telling
        # their nodes apart.
        "companions": [{"name": c["name"], "path": mapper.to_local(c["path"]), "indexed": c["indexed"]}
                       for c in described["sources"]],
        "companions_pending": described["pending"],
    }


# ---------------------------------------------------------------------------
# Locate / resolve / neighbourhood
# ---------------------------------------------------------------------------


@router.get("/api/locate")
def api_locate(request: Request, path: str, line: Optional[int] = Query(None, ge=1)):
    """The narrowest artifact enclosing ``path:line`` (function > class > file)."""
    guard(request)
    try:
        with _session() as session:
            return nb.locate(session, path, line, path_mapper())
    except nb.ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/entry-points")
def api_entry_points(request: Request, limit: int = Query(6, ge=1, le=30)):
    """Functions to start reading the served repository from (see nb.entry_points)."""
    guard(request)
    root = _index_services.get("repo_root")
    prefix = Path(root).as_posix().rstrip("/") + "/" if root else None
    try:
        with _session() as session:
            return {"entry_points": nb.entry_points(session, prefix, limit, path_mapper())}
    except Exception as exc:  # noqa: BLE001 - nothing indexed yet: no entry point
        return {"entry_points": [], "error": str(exc)}


@router.get("/api/resolve")
def api_resolve(request: Request, symbol: str, file: Optional[str] = None):
    """Every artifact named ``symbol`` (optionally within ``file``)."""
    guard(request)
    with _session() as session:
        matches = nb.resolve_symbol(session, symbol, file, path_mapper())
    if not matches:
        raise HTTPException(status_code=404, detail=f"No artifact named '{symbol}' in the graph.")
    return {"matches": matches}


@router.get("/api/neighborhood")
def api_neighborhood(
    request: Request,
    path: str,
    name: Optional[str] = None,
    line: Optional[int] = Query(None, ge=1),
    kind: Optional[str] = None,
    depth: int = Query(1, ge=1, le=nb.MAX_DEPTH),
    kinds: Optional[str] = Query(None, description="Comma-separated: CALLS,INHERITS,IMPORTS"),
    max_nodes: int = Query(nb.DEFAULT_MAX_NODES, ge=1, le=2000),
    per_node: int = Query(nb.DEFAULT_PER_NODE_LIMIT, ge=1, le=1000),
    hide_framework: bool = Query(False, description="Leave out base-library external types"),
    hide_tests: bool = Query(False, description="Leave out test code and test-only packages"),
):
    guard(request)
    started = time.perf_counter()
    try:
        with _session() as session:
            data = nb.neighborhood(
                session, path=path, name=name, line=line, kind=kind, depth=depth,
                kinds=_parse_kinds(kinds), max_nodes=max_nodes, per_node_limit=per_node,
                mapper=path_mapper(), hide_framework=hide_framework, hide_tests=hide_tests,
            )
    except nb.ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    data["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return data


# ---------------------------------------------------------------------------
# Live events (SSE)
# ---------------------------------------------------------------------------


class _Broker:
    """Fan out events from the shared log (and /api/focus) to SSE clients."""

    POLL_SECONDS = 0.5

    def __init__(self) -> None:
        self.queues: Set[asyncio.Queue] = set()
        # Set when the server shuts down: the SSE streams end, so the
        # server does not wait forever for the open pages' connections.
        self.closing = False
        self._task: Optional[asyncio.Task] = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.queues.add(q)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._tail_loop())
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.queues.discard(q)

    def close(self) -> None:
        """End every SSE stream (server shutdown)."""
        self.closing = True

    def broadcast(self, event: Dict[str, Any]) -> int:
        delivered = 0
        for q in list(self.queues):
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                pass
        return delivered

    async def _tail_loop(self) -> None:
        tail = graph_events.EventTail()
        while self.queues:
            for event in tail.poll():
                self.broadcast(_localise_event(event))
            await asyncio.sleep(self.POLL_SECONDS)


broker = _Broker()


def _localise_event(event: Dict[str, Any]) -> Dict[str, Any]:
    mapper = path_mapper()
    out = dict(event)
    files = event.get("files") or []
    out["indexed_files"] = files
    out["files"] = [mapper.to_local(f) for f in files]
    if event.get("repo"):
        out["indexed_repo"] = event["repo"]
        out["repo"] = mapper.to_local(event["repo"])
    return out


def _sse(event: Dict[str, Any]) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/api/events")
async def api_events(request: Request):
    """Server-Sent Events: ``graph:changed``, ``index:done``, ``focus``."""
    guard(request)
    queue = broker.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            yield _sse({"type": "hello", "ts": time.time(), "clients": len(broker.queues)})
            idle = 0.0
            while not broker.closing:
                if await request.is_disconnected():
                    break
                try:
                    # Short waits so a shutdown ends the stream within a second.
                    event = await asyncio.wait_for(queue.get(), timeout=1)
                    yield _sse(event)
                    idle = 0.0
                except asyncio.TimeoutError:
                    idle += 1
                    if idle >= 15:
                        yield ": ping\n\n"
                        idle = 0.0
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/focus")
async def api_focus(request: Request):
    """Ask already-open graph tabs to recentre on ``path:line``.

    Returns ``{"delivered": n}``; IDE integrations open a new tab only when
    no page is listening (``n == 0``).
    """
    _guard_local_write(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    path = body.get("path")
    if not isinstance(path, str) or not path:
        raise HTTPException(status_code=400, detail="'path' is required")
    line = body.get("line")
    event = {
        "type": "focus",
        "path": path,
        "line": int(line) if isinstance(line, (int, float, str)) and str(line).isdigit() else None,
        "symbol": body.get("symbol") if isinstance(body.get("symbol"), str) else None,
        "ts": time.time(),
    }
    return {"delivered": broker.broadcast(event)}


# ---------------------------------------------------------------------------
# Open in IDE (fallback for missing URL handlers)
# ---------------------------------------------------------------------------

# argv templates; {path} and {line} are substituted per argument (no shell).
IDE_COMMANDS: Dict[str, List[List[str]]] = {
    "vscode": [["code", "--goto", "{path}:{line}"]],
    "cursor": [["cursor", "--goto", "{path}:{line}"]],
    "rider": [
        ["rider", "--line", "{line}", "{path}"],
        ["rider64.exe", "--line", "{line}", "{path}"],
        ["open", "-na", "Rider.app", "--args", "--line", "{line}", "{path}"],
    ],
    "idea": [["idea", "--line", "{line}", "{path}"]],
    "zed": [["zed", "{path}:{line}"], ["open", "-a", "Zed", "--args", "{path}:{line}"]],
}


def _ide_argv(ide: str, path: str, line: int) -> Optional[List[str]]:
    override = os.getenv(f"CVIZ_IDE_COMMAND_{ide.upper()}")
    templates = [shlex.split(override, posix=os.name != "nt")] if override else IDE_COMMANDS.get(ide, [])
    for template in templates:
        if not template:
            continue
        exe = template[0]
        if exe == "open" and sys.platform != "darwin":
            continue
        if shutil.which(exe) is None and not Path(exe).is_file():
            continue
        return [part.replace("{path}", path).replace("{line}", str(line)) for part in template]
    return None


@router.post("/api/open")
async def api_open(request: Request):
    _guard_local_write(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    ide = str(body.get("ide") or os.getenv("CVIZ_DEFAULT_IDE", "vscode")).lower()
    path = body.get("path")
    try:
        line = max(1, int(body.get("line") or 1))
    except (TypeError, ValueError):
        line = 1
    if not isinstance(path, str) or not path:
        raise HTTPException(status_code=400, detail="'path' is required")
    if ide not in IDE_COMMANDS and not os.getenv(f"CVIZ_IDE_COMMAND_{ide.upper()}"):
        raise HTTPException(status_code=400, detail=f"Unknown IDE '{ide}'")

    local = Path(path)
    indexed = path_mapper().to_indexed(path) or path
    if not (is_readable_path(indexed) or is_path_allowed(local)):
        raise HTTPException(status_code=403, detail="Path is outside indexed repositories")
    if not local.is_file():
        raise HTTPException(status_code=404, detail="File not found on this machine")

    argv = _ide_argv(ide, str(local), line)
    if argv is None:
        raise HTTPException(
            status_code=501,
            detail=f"No command-line launcher found for '{ide}'. Install it on PATH "
                   f"or set CVIZ_IDE_COMMAND_{ide.upper()}.",
        )
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to launch {argv[0]}: {exc}")
    return {"ok": True, "command": argv[0]}


# ---------------------------------------------------------------------------
# Index update requested by `cviz update` (git hooks) while this server holds
# the database (LadybugDB accepts a single process).
# ---------------------------------------------------------------------------

_index_services: Dict[str, Any] = {"graph_builder": None, "watcher": None, "cvizignore_path": None,
                                   "repo_root": None}
_update_lock = __import__("threading").Lock()


def set_index_services(graph_builder, watcher=None, cvizignore_path: Optional[str] = None,
                       repo_root: Optional[str] = None) -> None:
    """Called by ``cviz graph``: the repository it serves, and what /api/update needs."""
    _index_services.update(graph_builder=graph_builder, watcher=watcher, cvizignore_path=cvizignore_path,
                           repo_root=repo_root)


@router.post("/api/update")
def api_update(request: Request, body: Dict[str, Any]):
    """Update the indexed repository containing ``path`` to the state of the disk.

    Only the files whose content differs from the graph are re-processed; a
    repository watched by this server goes through its watcher. 404 when the
    path is not in an indexed repository of this server's database.
    """
    _guard_local_write(request)
    builder = _index_services["graph_builder"]
    if builder is None:
        raise HTTPException(status_code=503, detail="This server cannot update the index.")
    raw = str(body.get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")
    target = Path(path_mapper().to_indexed(raw)).resolve().as_posix()
    roots = [r for r in indexed_repo_roots(max_age=0) if target == r or target.startswith(r + "/")]
    if not roots:
        raise HTTPException(status_code=404, detail=f"{raw} is not in an indexed repository of this server.")
    repo = max(roots, key=len)

    from ..core.watcher import incremental_update

    with _update_lock:
        watcher = _index_services["watcher"]
        handler = None
        if watcher is not None:
            handler = next((h for p, h in watcher.handlers.items()
                            if Path(p).resolve().as_posix() == repo), None)
        if handler is not None:
            result = handler.catch_up_with_disk()
            if result["mode"] != "none":
                graph_events.publish(graph_events.INDEX_DONE, repo=repo)
        else:
            result = incremental_update(builder, repo, cvizignore_path=_index_services["cvizignore_path"])
    return {**result, "repo": repo}


# ---------------------------------------------------------------------------
# Companion sources: other repositories / folders indexed with this one
# ---------------------------------------------------------------------------
# The list lives in <repo>/.code-visualizer/companions.json (core.companions).
# Adding or removing one changes what the next full index reads; the page
# offers that re-index, run here because this process holds the database.

_reindex: Dict[str, Any] = {"thread": None, "job_id": None, "error": None, "finished_at": None}


def _main_root() -> str:
    root = _index_services.get("repo_root")
    if not root:
        raise HTTPException(status_code=503, detail="This server does not serve a repository.")
    return root


def _file_counts(prefixes: List[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with _session() as session:
            for prefix in prefixes:
                result = session.run(
                    "MATCH (f:File) WHERE f.path STARTS WITH $prefix RETURN count(f) AS n",
                    prefix=prefix.rstrip("/") + "/",
                )
                rows = result.data() if hasattr(result, "data") else [dict(r) for r in result]
                out[prefix] = int(rows[0]["n"]) if rows else 0
    except Exception:  # noqa: BLE001 - counts are informative
        pass
    return out


def _reindex_state() -> Dict[str, Any]:
    thread = _reindex["thread"]
    running = bool(thread and thread.is_alive())
    state: Dict[str, Any] = {"running": running, "error": _reindex["error"],
                             "finished_at": _reindex["finished_at"]}
    builder = _index_services.get("graph_builder")
    job_id = _reindex["job_id"]
    if running and builder is not None and job_id:
        job = builder.job_manager.get_job(job_id)
        if job is not None:
            state.update(total=job.total_files or 0, done=job.processed_files or 0,
                         message=getattr(job, "status_message", None) or "")
    return state


def _sources_payload() -> Dict[str, Any]:
    from ..core import companions

    root = _main_root()
    mapper = path_mapper()
    described = companions.describe(root)
    counts = _file_counts([root] + [s["path"] for s in described["sources"]])
    for source in described["sources"]:
        source["display_path"] = companions.display_path(source["path"], root)
        source["files"] = counts.get(source["path"]) if source["indexed"] else None
        source["path"] = mapper.to_local(source["path"])
    local = mapper.to_local(root)
    return {
        "main": {"name": Path(local).name, "path": local, "files": counts.get(root)},
        **described,
        "can_reindex": _index_services.get("graph_builder") is not None,
        "reindex": _reindex_state(),
    }


@router.get("/api/sources")
def api_sources(request: Request):
    """The repository served and its companion sources, with their state."""
    guard(request)
    return _sources_payload()


@router.get("/api/sources/suggestions")
def api_sources_suggestions(request: Request):
    """Folders next to the repository, those providing packages it uses first."""
    guard(request)
    from ..core import companions

    root = _main_root()
    out = companions.suggestions(root)
    for s in out:
        s["display_path"] = companions.display_path(s["path"], root)
    return {"suggestions": out}


@router.post("/api/sources")
def api_sources_change(request: Request, body: Dict[str, Any]):
    """``{"action": "add" | "remove", "path": ...}``; takes effect at the next re-index."""
    _guard_local_write(request)
    from ..core import companions

    root = _main_root()
    action = str(body.get("action") or "")
    raw = str(body.get("path") or "").strip()
    path = path_mapper().to_indexed(raw) or raw
    if action == "add":
        try:
            companions.add(root, path)
        except companions.CompanionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    elif action == "remove":
        if not companions.remove(root, path):
            raise HTTPException(status_code=404, detail=f"{raw} is not a companion source of this repository.")
    else:
        raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")
    return _sources_payload()


def reindex_with_companions(builder, repo_root: str, job_id: Optional[str] = None,
                            cvizignore_path: Optional[str] = None) -> None:
    """Delete the repository and its companion sources (current and former)
    from the graph, then index them again in one pass."""
    from ..core import companions

    stale = {Path(p).resolve().as_posix() for p in companions.indexed_roots(repo_root)}
    stale |= {p.resolve().as_posix() for p in companions.companion_roots(repo_root)}
    indexed = set(indexed_repo_roots(max_age=0))
    for path in sorted(stale | {Path(repo_root).resolve().as_posix()}):
        if path in indexed:
            builder.delete_repository_from_graph(path)
    asyncio.run(builder.build_graph_from_path_async(
        Path(repo_root), is_dependency=False, job_id=job_id, cvizignore_path=cvizignore_path))
    job = builder.job_manager.get_job(job_id) if job_id else None
    status = getattr(getattr(job, "status", None), "value", getattr(job, "status", None))
    if job is not None and str(status).lower() in ("failed", "cancelled"):
        raise RuntimeError("; ".join(job.errors or []) or "indexing failed")


def _run_reindex(repo_root: str, job_id: str) -> None:
    builder = _index_services["graph_builder"]
    watcher = _index_services.get("watcher")
    handlers = list(getattr(watcher, "handlers", {}).values()) if watcher is not None else []
    _reindex["error"] = None
    with _update_lock:
        # Hold the watcher back: its incremental updates would race the rebuild.
        held = []
        try:
            for handler in handlers:
                lock = getattr(handler, "_update_lock", None)
                if lock is not None:
                    lock.acquire()
                    held.append(lock)
            reindex_with_companions(builder, repo_root, job_id, _index_services.get("cvizignore_path"))
            for handler in handlers:
                handler.imports_map = {}  # rebuilt with the companions at the next change
        except Exception as exc:  # noqa: BLE001 - reported to the page
            _reindex["error"] = str(exc) or exc.__class__.__name__
        finally:
            for lock in held:
                lock.release()
            _repo_cache["ts"] = 0.0
            _reindex["finished_at"] = time.time()
    graph_events.publish(graph_events.INDEX_DONE, repo=Path(repo_root).resolve().as_posix())
    try:
        from .ai_api import catch_up_vectorization

        catch_up_vectorization(delay=0)
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/sources/reindex", status_code=202)
def api_sources_reindex(request: Request, body: Optional[Dict[str, Any]] = None):
    """Re-index the repository with its companion sources, in the background."""
    _guard_local_write(request)
    import threading

    builder = _index_services.get("graph_builder")
    if builder is None:
        raise HTTPException(status_code=503, detail="This server cannot update the index.")
    root = _main_root()
    thread = _reindex["thread"]
    if not (thread and thread.is_alive()):
        _reindex["job_id"] = builder.job_manager.create_job(root)
        thread = threading.Thread(target=_run_reindex, args=(root, _reindex["job_id"]),
                                  name="cviz-reindex", daemon=True)
        _reindex["thread"] = thread
        thread.start()
    return _sources_payload()


# ---------------------------------------------------------------------------
# Agent tools (agent_tools): what the MCP server forwards here when this
# process holds the database (LadybugDB accepts a single process).
# ---------------------------------------------------------------------------


@router.post("/api/agent/{tool}")
def api_agent_tool(tool: str, request: Request, body: Dict[str, Any]):
    _guard_local_write(request)
    from .. import agent_tools

    if tool not in agent_tools.TOOL_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool}")
    from ..semantic.search import engine

    return agent_tools.call_tool(_db_manager(), tool, body or {}, path_mapper(), engine=engine())


# ---------------------------------------------------------------------------
# Changes against a target branch (changes.py): the "Changes" view
# ---------------------------------------------------------------------------


def _changes_root() -> str:
    root = _index_services.get("repo_root")
    if not root:
        raise HTTPException(status_code=409, detail="This server serves no repository (start it with cviz graph --watch <repo>).")
    return root


@router.get("/api/changes/branches")
def api_changes_branches(request: Request):
    """Branches to compare the current state with, and the default target."""
    guard(request)
    from . import changes

    root = _changes_root()
    try:
        return dict(changes.branches(path_mapper().to_local(root) or root), available=True)
    except changes.GitError as exc:
        return {"available": False, "error": str(exc), "branches": [], "current": None, "default": None}


@router.post("/api/changes/fetch")
def api_changes_fetch(request: Request, body: Optional[Dict[str, Any]] = None):
    """`git fetch --all --prune`: the remote branches as they are now."""
    _guard_local_write(request)
    from . import changes

    root = path_mapper().to_local(_changes_root()) or _changes_root()
    try:
        changes.fetch(root)
        return dict(changes.branches(root), available=True)
    except changes.GitError as exc:
        raise HTTPException(status_code=502, detail=f"git fetch failed: {exc}")


@router.get("/api/changes")
def api_changes(
    request: Request,
    target: str,
    hide_framework: bool = Query(False),
    hide_tests: bool = Query(False),
):
    """Entities the current state adds or modifies since it left *target*, and
    their callers (see changes.py)."""
    guard(request)
    from . import changes

    root = _changes_root()
    try:
        with _session() as session:
            return changes.compute(session, root, target, path_mapper(),
                                   hide_framework=hide_framework, hide_tests=hide_tests,
                                   builder=_index_services.get("graph_builder"))
    except changes.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
