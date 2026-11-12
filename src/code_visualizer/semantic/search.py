"""Hybrid search over the code artifacts of the graph.

The two rankings (``semantic/graph_index``):

* semantic - ``QUERY_VECTOR_INDEX``: nearest artifact vectors (HNSW, cosine)
  to the query vector of the configured model;
* keyword  - BM25 (``graph_index.keyword_candidates``) over identifier-split words
  (``computeDiscount`` and ``compute_discount`` both give
  ``compute``/``discount``) of the name, path, docstring and source.

Python only fuses the two ranked lists (Reciprocal Rank Fusion), puts an
artifact named by an exact identifier of the query first and keeps the top
``limit``. Without vectors (embedder unavailable, nothing vectorized yet) the
search degrades to keywords only and says so (``mode``).
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..viz.neighborhood import node_id
from . import settings as ai_settings
from .embedders import Embedder, EmbeddingError, get_embedder, probe

RRF_K = 60
CANDIDATES_PER_LIST = 50
SNIPPET_LINES = 4

STOPWORDS = frozenset("""
a an and are as at be by do does for from how i in is it of on or the to what where which who why with
au aux avec ce ces cette comment dans de des du elle en est et il je la le les leur lui mais me ne ou où
par pas pour qu que quel quelle quels qui sa se ses son sur ta te tu un une vos votre y fait faire
""".split())

_SPLIT = re.compile(r"[^0-9A-Za-z]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CODE_LIKE = re.compile(r"_|[a-z][A-Z]|[A-Z]{2,}[a-z]")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def tokenize(text: str) -> List[str]:
    """Lower-case words of ``text``, identifiers split, stop words dropped."""
    out: List[str] = []
    for chunk in _SPLIT.split(_fold(text or "")):
        if not chunk:
            continue
        for part in _CAMEL.findall(chunk):
            token = part.lower()
            if len(token) >= 2 and token not in STOPWORDS:
                out.append(token)
    return out


def code_identifiers(query: str) -> List[str]:
    """Words of the query that name code.

    Only words that look like code count (``snake_case``, ``camelCase``,
    ``PascalCase``, or anything in backticks): "stream" in a sentence is a
    word, ``stream`` or ``EventStream`` is a symbol.
    """
    quoted = set(re.findall(r"`([^`]+)`", query))
    found: List[str] = []
    for ident in _IDENT.findall(query):
        if (ident in quoted or _CODE_LIKE.search(ident)) and ident not in found:
            found.append(ident)
    return found


@dataclass
class Item:
    """An artifact found by the search."""
    id: str
    kind: str
    name: str
    path: str
    line: Optional[int]
    end_line: Optional[int]
    text: str  # "<kind> <name>\n<path>\n<docstring>\n<source>" (chat excerpts)


@dataclass
class Hit:
    item: Item
    score: float
    semantic: Optional[float]
    keyword: Optional[float]
    exact: bool = False


def fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> Dict[str, float]:
    """Reciprocal Rank Fusion of ranked id lists."""
    fused: Dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank + 1)
    return fused


def _key(row: Dict[str, Any]) -> str:
    line = row.get("line")
    return node_id(row["kind"], row.get("path"), row.get("name"), int(line) if line is not None else None)


def _item(row: Dict[str, Any], reader) -> Item:
    from .indexer import build_text

    line = int(row["line"]) if row.get("line") is not None else None
    end_line = int(row["end_line"]) if row.get("end_line") is not None else None
    path = str(row.get("path") or "").replace("\\", "/")
    source = row.get("source") or reader.lines(path, line, end_line)
    text = build_text(row["kind"], row.get("name") or "", path, row.get("docstring"), source)
    return Item(id=_key(row), kind=row["kind"], name=row.get("name") or "", path=path,
                line=line, end_line=end_line, text=text)


SessionFactory = Callable[[], Any]


class SearchEngine:
    """Hybrid search; keeps the query embedder loaded between searches."""

    def __init__(self, session_factory: Optional[SessionFactory] = None):
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._embedder: Optional[Embedder] = None
        self._embedder_id = ""

    def _session(self):
        if self._session_factory is None:
            from ..viz.graph_api import _session

            return _session()
        return self._session_factory()

    def _get_embedder(self, embed_settings: ai_settings.EmbedSettings) -> Embedder:
        key = embed_settings.model_id + embed_settings.url + embed_settings.api_key
        with self._lock:
            if self._embedder is None or self._embedder_id != key:
                self._embedder = get_embedder(embed_settings)
                self._embedder_id = key
            return self._embedder

    def search(self, query: str, limit: int = 5, repos: Optional[Sequence[str]] = None,
               embed_settings: Optional[ai_settings.EmbedSettings] = None) -> Dict[str, Any]:
        from . import graph_index
        from .indexer import SourceReader

        query = (query or "").strip()
        embed_settings = embed_settings or ai_settings.embed_settings()
        model_id = embed_settings.model_id
        prefix = (repos[0].replace("\\", "/").rstrip("/") + "/") if repos else None
        result: Dict[str, Any] = {"query": query, "hits": [], "mode": "hybrid", "model": model_id,
                                  "warning": None, "indexed": 0, "vectorized": 0}

        with self._session() as session:
            ready = graph_index.search_ready(session)
            if not ready["keyword"]:
                result["mode"] = "none"
                result["warning"] = "nothing indexed for search yet (Settings > AI > Vectorize)"
                return result
            counts = graph_index.stats(session, model_id, prefix)
            result["indexed"], result["vectorized"] = counts["items"], counts["embedded"]
            if not query:
                return result

            keyword_rows = graph_index.keyword_candidates(
                session, " ".join(tokenize(query)) or query, CANDIDATES_PER_LIST, prefix)
            semantic_rows: List[Dict[str, Any]] = []
            if not (ready["vector"] and counts["embedded"]):
                result["mode"] = "keyword"
                result["warning"] = ("no vectors for this model yet: keyword search only "
                                     "(Settings > AI > Vectorize)")
            else:
                try:
                    problem = probe(embed_settings)
                    if problem:
                        raise EmbeddingError(problem)
                    vector = self._get_embedder(embed_settings).embed_query(query)
                    semantic_rows = graph_index.vector_candidates(
                        session, vector, model_id, CANDIDATES_PER_LIST, prefix)
                except EmbeddingError as exc:
                    result["mode"] = "keyword"
                    result["warning"] = f"semantic search unavailable ({exc}): keyword search only"
            exact_rows = graph_index.named(session, code_identifiers(query), prefix)

        rows: Dict[str, Dict[str, Any]] = {}
        for row in exact_rows + semantic_rows + keyword_rows:
            rows.setdefault(_key(row), row)
        semantic: List[Tuple[str, float]] = [(_key(r), 1.0 - float(r["distance"])) for r in semantic_rows]
        keyword: List[Tuple[str, float]] = [(_key(r), float(r["score"])) for r in keyword_rows]
        fused = fuse([[k for k, _ in ranking] for ranking in (semantic, keyword) if ranking])
        exact = list(dict.fromkeys(_key(r) for r in exact_rows))
        order = exact + [k for k, _ in sorted(fused.items(), key=lambda kv: -kv[1]) if k not in exact]

        sem_map, kw_map, reader = dict(semantic), dict(keyword), SourceReader()
        result["hits"] = [
            Hit(_item(rows[k], reader), fused.get(k, 0.0), sem_map.get(k), kw_map.get(k), k in exact)
            for k in order[: max(1, limit)]
        ]
        return result


_ENGINE: Optional[SearchEngine] = None
_ENGINE_LOCK = threading.Lock()


def engine() -> SearchEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = SearchEngine()
        return _ENGINE


def hit_to_dict(hit: Hit, to_local=lambda p: p) -> Dict[str, Any]:
    item = hit.item
    lines = item.text.split("\n")
    snippet = "\n".join(lines[2:2 + SNIPPET_LINES])  # skip "Kind name" and the path header
    return {
        "id": item.id,
        "kind": item.kind,
        "name": item.name,
        "label": item.name,
        "path": to_local(item.path),
        "indexed_path": item.path,
        "line": item.line,
        "end_line": item.end_line,
        "score": round(hit.score, 5),
        "semantic": round(hit.semantic, 4) if hit.semantic is not None else None,
        "keyword": round(hit.keyword, 3) if hit.keyword is not None else None,
        "exact": hit.exact,
        "snippet": snippet[:400],
    }
