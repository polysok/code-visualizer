"""HTTP API of the natural-language search (graph page chat and settings).

GET  /api/search          hybrid search, no LLM
POST /api/chat            search + LLM re-ranking / optional answer
GET  /api/ai/status       settings (secrets masked) + vectorization state
POST /api/ai/config       save settings                 (this machine only)
POST /api/ai/models       models served by an LLM URL   (this machine only)
POST /api/ai/embed        vectorize the indexed repos   (this machine only)
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..semantic import graph_index
from ..semantic import settings as ai_settings
from ..semantic.chat import LlmError, chat, list_models
from ..semantic.embedders import probe
from ..semantic.search import engine, hit_to_dict
from .graph_api import _guard_local_write, _session, guard, indexed_repo_roots, path_mapper

router = APIRouter()


def _repos(repo: Optional[str]) -> Optional[List[str]]:
    if not repo:
        return None
    return [path_mapper().to_indexed(repo).replace("\\", "/").rstrip("/")]


@router.get("/api/search")
def api_search(request: Request, q: str = Query(..., min_length=1, max_length=2000),
               limit: int = Query(5, ge=1, le=20), repo: Optional[str] = None):
    """Hybrid (semantic + keyword) search; at most ``limit`` artifacts."""
    guard(request)
    mapper = path_mapper()
    try:
        result = engine().search(q, limit=limit, repos=_repos(repo))
    except Exception as exc:  # noqa: BLE001 - surface index problems to the page
        raise HTTPException(status_code=500, detail=f"search failed: {exc}")
    result["hits"] = [hit_to_dict(h, mapper.to_local) for h in result["hits"]]
    return result


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=8000)


class ChatBody(BaseModel):
    messages: List[ChatMessage] = Field(min_length=1, max_length=40)
    repo: Optional[str] = None


@router.post("/api/chat")
def api_chat(request: Request, body: ChatBody):
    guard(request)
    mapper = path_mapper()
    try:
        reply = chat([m.model_dump() for m in body.messages], repos=_repos(body.repo))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"chat failed: {exc}")
    reply["hits"] = [hit_to_dict(h, mapper.to_local) for h in reply["hits"]]
    reply["search_hits"] = [hit_to_dict(h, mapper.to_local) for h in reply["search_hits"]]
    return reply


# ---------------------------------------------------------------------------
# Settings and vectorization
# ---------------------------------------------------------------------------

_embed_job: Dict[str, Any] = {"running": False, "last": None}
_embed_lock = threading.Lock()


@router.get("/api/ai/status")
def api_ai_status(request: Request) -> Dict[str, Any]:
    guard(request)
    embed = ai_settings.embed_settings()
    stats = {"items": 0, "embedded": 0}
    repos: List[Dict[str, Any]] = []
    index_error = None
    try:
        roots = indexed_repo_roots()
        with _session() as session:
            if graph_index.has_search_texts(session):
                stats = graph_index.stats(session, embed.model_id)
                repos = [{"repo": r, **graph_index.stats(session, embed.model_id, r + "/")}
                         for r in roots]
    except Exception as exc:  # noqa: BLE001 - the status must always answer
        index_error = str(exc)
    return {
        "config": ai_settings.public_config(),
        "enabled": ai_settings.enabled(),
        "embed_model": embed.model_id,
        "embed_problem": probe(embed),
        "llm_configured": ai_settings.llm_settings().configured,
        "stats": stats,
        "repos": repos,
        "index_error": index_error,
        "job": dict(_embed_job),
    }


@router.post("/api/ai/config")
def api_ai_config(request: Request, body: Dict[str, Optional[str]]):
    _guard_local_write(request)
    try:
        ai_settings.save(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"config": ai_settings.public_config()}


class ModelsBody(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    api_key: Optional[str] = Field(None, max_length=500)
    use_saved_key: Optional[str] = Field(None, pattern="^(llm|embed)$")


@router.post("/api/ai/models")
def api_ai_models(request: Request, body: ModelsBody):
    """Models offered by a server, to fill the model pickers of Settings > AI."""
    _guard_local_write(request)
    key = body.api_key or ""
    if not key and body.use_saved_key == "llm":
        key = ai_settings.llm_settings().api_key
    elif not key and body.use_saved_key == "embed":
        key = ai_settings.embed_settings().api_key
    try:
        models = list_models(ai_settings.LlmSettings("other", body.url.rstrip("/"), "", key))
    except LlmError as exc:
        raise HTTPException(status_code=502, detail=f"cannot list models: {exc}")
    return {"models": models}


def _run_embed(repos: List[str]) -> None:
    from ..semantic.indexer import sync

    results = []
    try:
        for repo in repos:
            results.append(sync(_session, repo))
    except Exception as exc:  # noqa: BLE001
        results.append({"repo": None, "error": str(exc)})
    finally:
        with _embed_lock:
            _embed_job.update(running=False, last=results)


def _start_embed(repos: List[str]) -> bool:
    """Vectorize *repos* in a background thread; False when one already runs."""
    with _embed_lock:
        if _embed_job["running"]:
            return False
        _embed_job.update(running=True, last=None)
    threading.Thread(target=_run_embed, args=(repos,), name="cviz-embed", daemon=True).start()
    return True


def repos_to_vectorize() -> List[str]:
    """Indexed repositories with artifacts not vectorized with the current model.

    A repository indexed without vectors (``cviz index`` without ``--embed``,
    or re-indexed with ``--force``) has no search text yet: ``items`` is 0.
    """
    model_id = ai_settings.embed_settings().model_id
    roots = indexed_repo_roots(max_age=0)
    if not roots:
        return []
    with _session() as session:
        if not graph_index.has_search_texts(session):
            return roots
        pending = []
        for root in roots:
            s = graph_index.stats(session, model_id, root + "/")
            if not s["items"] or s["embedded"] < s["items"]:
                pending.append(root)
        return pending


def catch_up_vectorization(delay: float = 1.5) -> None:
    """At server start-up, vectorize what the index left out (embed:* events).

    Does nothing unless SEMANTIC_SEARCH is on and the embedding model is
    usable. The delay lets the page connect to /api/events first.
    """
    def _check() -> None:
        time.sleep(delay)
        try:
            if not ai_settings.enabled() or probe(ai_settings.embed_settings()):
                return
            pending = repos_to_vectorize()
            if pending:
                _start_embed(pending)
        except Exception as exc:  # noqa: BLE001 - the server must start regardless
            from ..utils.debug_log import warning_logger
            warning_logger(f"[SEARCH] start-up vectorization skipped: {exc}")

    threading.Thread(target=_check, name="cviz-embed-check", daemon=True).start()


class EmbedBody(BaseModel):
    repo: Optional[str] = None


@router.post("/api/ai/embed", status_code=202)
def api_ai_embed(request: Request, body: EmbedBody):
    """Vectorize the indexed repositories in the background (embed:* events)."""
    _guard_local_write(request)
    problem = probe(ai_settings.embed_settings())
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    repos = _repos(body.repo) or indexed_repo_roots(max_age=0)
    if not repos:
        raise HTTPException(status_code=404, detail="No indexed repository.")
    if not _start_embed(repos):
        raise HTTPException(status_code=409, detail="A vectorization is already running.")
    return {"started": repos}
