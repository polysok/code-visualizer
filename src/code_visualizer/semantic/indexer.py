"""Write the search data of code artifacts into the graph and vectorize them.

``sync(session_factory, repo)`` reads the artifacts of ``repo`` (or only those
of ``paths``), stores their keyword text on the nodes, then embeds the
artifacts whose text or model changed. LadybugDB's ``vector`` indexes,
created here when missing, follow every write on their own; the keyword
ranking (BM25) reads the stored words (semantic/graph_index.py).

Called after ``cviz index``, by the watcher for the files it re-indexes, by
``cviz embed`` and by the page (Settings > AI > Vectorize). It must run in the
process that holds the database (LadybugDB accepts a single process).
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ..core import graph_events
from ..viz.neighborhood import MODULE_PSEUDO_FUNCTION
from . import graph_index
from . import settings as ai_settings
from .embedders import EmbeddingError, get_embedder, probe
from .search import tokenize

EMBED_PROGRESS = "embed:progress"
EMBED_DONE = "embed:done"

MAX_TEXT_CHARS = 1800        # ~ 512 tokens of code: the e5 context window
MAX_KEYWORD_CHARS = 8000     # source kept in the keyword text
MAX_FALLBACK_LINES = 120     # source read from disk when the graph has none
NAME_WEIGHT = 3              # the name's words count as many times in BM25
EMBED_CHUNK = 64             # texts per embedding call / write / progress event

_running = threading.Lock()  # one vectorization at a time per process

ProgressFn = Callable[[int, int], None]


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


class SourceReader:
    """Reads artifact source from disk when the graph does not store it."""

    def __init__(self) -> None:
        self._files: Dict[str, Optional[List[str]]] = {}

    def lines(self, path: str, start: Optional[int], end: Optional[int]) -> str:
        if not start:
            return ""
        if path not in self._files:
            try:
                self._files[path] = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._files[path] = None
        content = self._files[path]
        if not content:
            return ""
        stop = min(len(content), end or start, start + MAX_FALLBACK_LINES - 1)
        return "\n".join(content[start - 1:stop])


def build_text(kind: str, name: str, rel_path: str, docstring: Optional[str], source: Optional[str]) -> str:
    """Text given to the embedding model."""
    parts = [f"{kind} {name}", rel_path]
    if docstring and docstring.strip() and docstring.strip() not in (source or ""):
        parts.append(docstring.strip())
    if source:
        parts.append(source.strip())
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def keyword_text(name: str, rel_path: str, docstring: Optional[str], source: Optional[str]) -> str:
    """Words ranked by the keyword search (BM25): identifiers split (``computeDiscount``
    gives ``compute discount``), the name's words repeated to weigh more."""
    words = tokenize(name) * NAME_WEIGHT + tokenize(rel_path)
    if docstring:
        words += tokenize(docstring)
    if source:
        words += tokenize(source[:MAX_KEYWORD_CHARS])
    return " ".join(words)


@dataclass
class Artifact:
    uid: str
    kind: str
    name: str
    path: str
    line: Optional[int]
    text: str            # embedded
    keywords: str        # stored in search_text
    stored_text: Optional[str]
    stored_hash: Optional[str]


def text_hash(model_id: str, text: str) -> str:
    return graph_index.model_prefix(model_id) + hashlib.sha1(text.encode("utf-8")).hexdigest()


# Style sheets: the CSS parser turns every rule into a node (a vendored
# swagger-ui.css alone gave 6,000), and no plain-words question is answered
# by a selector. Left out of the search, they cost most of the vectorization.
STYLE_EXTENSIONS = (".css", ".scss", ".sass", ".less", ".styl")


def to_artifacts(rows: Iterable[Dict[str, Any]], repo: str,
                 include_dependencies: bool = False) -> List[Artifact]:
    """Searchable artifacts among *rows*. Dependency code is left out, unless
    the repository is itself one (a companion source, indexed to be read)."""
    reader = SourceReader()
    root = repo.rstrip("/") + "/"
    out: List[Artifact] = []
    for row in rows:
        name, path, uid = row.get("name"), row.get("path"), row.get("uid")
        if not path or not name or not uid or name == MODULE_PSEUDO_FUNCTION:
            continue
        if row.get("dep") and not include_dependencies:
            continue
        path = _norm(path)
        if path.lower().endswith(STYLE_EXTENSIONS):
            continue
        kind = row.get("kind") or "Function"
        line = int(row["line"]) if row.get("line") is not None else None
        end_line = int(row["end_line"]) if row.get("end_line") is not None else None
        source = row.get("source") or reader.lines(path, line, end_line)
        rel = path[len(root):] if path.startswith(root) else path
        out.append(Artifact(
            uid=uid, kind=kind, name=name, path=path, line=line,
            text=build_text(kind, name, rel, row.get("docstring"), source),
            keywords=keyword_text(name, rel, row.get("docstring"), source),
            stored_text=row.get("search_text"), stored_hash=row.get("search_hash"),
        ))
    return out


def _publish(event_type: str, repo: str, **extra: Any) -> None:
    graph_events.publish(event_type, repo=repo, **extra)


def _by_label(pairs: Iterable[tuple]) -> Dict[str, List[Dict[str, Any]]]:
    """Group ``(artifact, row)`` pairs by label. A uid is only unique within
    its label (a minified ``T`` can be both a Function and a Class)."""
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for artifact, row in pairs:
        grouped[artifact.kind].append(row)
    return grouped


def store_keywords(session, artifacts: Sequence[Artifact]) -> int:
    """Write the keyword text of the artifacts whose text changed."""
    changed = [(a, {"uid": a.uid, "text": a.keywords}) for a in artifacts if a.stored_text != a.keywords]
    for label, rows in _by_label(changed).items():
        graph_index.write_texts(session, label, rows)
    return len(changed)


def embed_pending(session_factory: Callable[[], Any], artifacts: Sequence[Artifact], repo: str,
                  embed_settings: ai_settings.EmbedSettings, progress: Optional[ProgressFn] = None,
                  publish: bool = True) -> int:
    """Embed the artifacts whose stored hash does not match their text and model.

    Returns the number of texts actually embedded (vectors taken back from
    the stash do not count)."""
    model_id = embed_settings.model_id
    pending = [a for a in artifacts if a.stored_hash != text_hash(model_id, a.text)]
    if not pending:
        return 0
    # Artifacts recreated with an unchanged text get their previous vector back.
    reused = graph_index.take_stashed({text_hash(model_id, a.text) for a in pending})
    if reused:
        pairs = [(a, {"uid": a.uid, "hash": h, "vec": reused[h]})
                 for a in pending if (h := text_hash(model_id, a.text)) in reused]
        dim = len(pairs[0][1]["vec"])
        with session_factory() as session:
            graph_index.ensure_embedding_column(session, dim, model_id)
            for label, label_rows in _by_label(pairs).items():
                graph_index.write_vectors(session, label, dim, label_rows)
        pending = [a for a in pending if text_hash(model_id, a.text) not in reused]
        if not pending:
            with session_factory() as session:
                graph_index.ensure_indexes(session, dim)
            return 0
    embedder = get_embedder(embed_settings)
    total, done, last_event, dim = len(pending), 0, 0.0, None
    for i in range(0, total, EMBED_CHUNK):
        chunk = pending[i:i + EMBED_CHUNK]
        vectors = embedder.embed_documents([a.text for a in chunk])
        pairs = [(a, {"uid": a.uid, "hash": text_hash(model_id, a.text), "vec": [float(x) for x in v]})
                 for a, v in zip(chunk, vectors)]
        with session_factory() as session:
            if dim is None:
                dim = len(pairs[0][1]["vec"])
                graph_index.ensure_embedding_column(session, dim, model_id)
            for label, label_rows in _by_label(pairs).items():
                graph_index.write_vectors(session, label, dim, label_rows)
        done += len(chunk)
        if progress:
            progress(done, total)
        now = time.time()
        if publish and (now - last_event > 1.0 or done == total):
            _publish(EMBED_PROGRESS, repo, done=done, total=total, model=model_id)
            last_event = now
    with session_factory() as session:
        graph_index.ensure_indexes(session, dim)
    return done


def _is_dependency_repo(session, repo: str) -> bool:
    try:
        result = session.run("MATCH (r:Repository {path: $path}) RETURN r.is_dependency AS dep", path=repo)
        rows = result.data() if hasattr(result, "data") else [dict(r) for r in result]
    except Exception:  # noqa: BLE001
        return False
    return bool(rows and rows[0].get("dep"))


def sync(session_factory: Callable[[], Any], repo: str, paths: Optional[Sequence[str]] = None,
         embed_settings: Optional[ai_settings.EmbedSettings] = None,
         progress: Optional[ProgressFn] = None, publish: bool = True) -> Dict[str, Any]:
    """Refresh the search data of ``repo`` (or of ``paths``) in the graph.

    Returns ``{"repo", "items", "embedded", "error"}``. Failures are reported,
    not raised: indexing must not fail because of the search. Without a
    usable embedding model the keyword search is still kept up to date.
    """
    repo = _norm(repo).rstrip("/")
    paths = [_norm(p) for p in paths] if paths is not None else None
    embed_settings = embed_settings or ai_settings.embed_settings()
    result: Dict[str, Any] = {"repo": repo, "items": 0, "embedded": 0, "error": None}
    try:
        with session_factory() as session:
            graph_index.ensure_extensions(session)
            graph_index.ensure_text_columns(session)
            rows = graph_index.collect(session, repo + "/", paths)
            artifacts = to_artifacts(rows, repo, include_dependencies=_is_dependency_repo(session, repo))
            store_keywords(session, artifacts)
            graph_index.ensure_indexes(session, graph_index.active_dim(session))
        result["items"] = len(artifacts)
        problem = probe(embed_settings)
        if problem:
            result["error"] = problem
        else:
            with _running:
                result["embedded"] = embed_pending(session_factory, artifacts, repo, embed_settings,
                                                   progress, publish)
    except (EmbeddingError, graph_index.SearchIndexError) as exc:
        result["error"] = str(exc)
    finally:
        # Writes to the search indexes must not linger in the WAL: one left
        # by a crash cannot be replayed (core/database_embedded_kuzu.py).
        try:
            with session_factory() as session:
                session.run_native("CHECKPOINT")
        except Exception:  # noqa: BLE001 - best effort
            pass
        if publish:
            _publish(EMBED_DONE, repo, items=result["items"], embedded=result["embedded"],
                     error=result["error"], model=embed_settings.model_id)
    return result
