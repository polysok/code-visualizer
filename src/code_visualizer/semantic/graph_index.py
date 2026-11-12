"""Search data and indexes stored in the LadybugDB graph itself.

Every code artifact node (``ARTIFACT_LABELS``) carries three properties:

``search_text``       identifier-split words of its name, path, docstring and
                      source; ranked by BM25 in Python (``keyword_candidates``).
``search_embedding_<N>``  its vector, a fixed-size ``FLOAT[N]`` column (N = the
                      embedding model's dimension); indexed by the ``vector``
                      extension (HNSW, cosine). One column per dimension:
                      LadybugDB 0.19 cannot index a column dropped and re-added
                      under the same name once the database has checkpointed
                      ("Miss cached column"), so switching models deactivates
                      the old column (index dropped, values cleared) instead.
``search_hash``       ``<model id>|<sha1 of the embedded text>``: tells which
                      model produced the vector and whether the text changed.

The vector index is maintained by the database on every insert, update and
delete (``QUERY_VECTOR_INDEX``). LadybugDB's ``fts`` index is not used: in
0.19 it is corrupted by multi-row writes (one ``UNWIND ... CREATE``), after
which deleting the nodes fails ("FTS index ... is inconsistent"), and a
dropped one can leave an internal table that blocks its re-creation. The
keyword ranking is a BM25 over the ``search_text`` words, computed here from
a per-process copy of the words, rebuilt after each write of search texts.
Statements go through ``run_native``: they are LadybugDB Cypher, not the
Neo4j dialect the rest of the code writes.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter, OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..viz.neighborhood import ARTIFACT_LABELS

SEARCH_LABELS = ARTIFACT_LABELS
EXTENSIONS = ("vector",)
TEXT_COLUMNS = (("search_text", "STRING"), ("search_hash", "STRING"))
BM25_K1 = 1.2
BM25_B = 0.75

_VEC_INDEX = re.compile(r"^cviz_vec_(\w+)_(\d+)$")


class SearchIndexError(RuntimeError):
    """The search extensions or indexes cannot be used."""


def embedding_column(dim: int) -> str:
    return f"search_embedding_{int(dim)}"


def vec_index(label: str, dim: int) -> str:
    return f"cviz_vec_{label}_{int(dim)}"


def model_prefix(model_id: str) -> str:
    return f"{model_id}|"


def _native(session, statement: str, **params) -> List[Dict[str, Any]]:
    return session.run_native(statement, **params).data()


def _ignorable(exc: Exception) -> bool:
    text = str(exc).lower()
    return "already" in text or "duplicate" in text


# ---------------------------------------------------------------------------
# Schema: extensions, columns, indexes
# ---------------------------------------------------------------------------

def ensure_extensions(session) -> None:
    """Load the vector and fts extensions, installing them on first use.

    ``INSTALL`` downloads the extension once per machine
    (extension.ladybugdb.com); behind a proxy that blocks it the search
    cannot work, so the error says why.
    """
    for ext in EXTENSIONS:
        try:
            _native(session, f"LOAD {ext}")
            continue
        except Exception:
            pass
        try:
            _native(session, f"INSTALL {ext}")
            _native(session, f"LOAD {ext}")
        except Exception as exc:
            raise SearchIndexError(
                f"LadybugDB extension '{ext}' cannot be installed ({exc}). It is downloaded "
                "once from extension.ladybugdb.com: allow that host, then retry."
            ) from exc


def _columns(session, label: str) -> Dict[str, str]:
    rows = _native(session, f"CALL TABLE_INFO('{label}') RETURN name, type")
    return {r["name"]: r["type"] for r in rows}


def ensure_text_columns(session) -> None:
    for label in SEARCH_LABELS:
        existing = _columns(session, label)
        for column, column_type in TEXT_COLUMNS:
            if column in existing:
                continue
            try:
                _native(session, f"ALTER TABLE `{label}` ADD {column} {column_type}")
            except Exception as exc:
                if not _ignorable(exc):
                    raise


def indexes(session) -> set:
    return {r["index_name"] for r in _native(session, "CALL SHOW_INDEXES() RETURN index_name")}


def active_dim(session) -> Optional[int]:
    """Dimension of the vector indexes in place, None when there are none."""
    for name in indexes(session):
        match = _VEC_INDEX.match(name)
        if match:
            return int(match.group(2))
    return None


def ensure_embedding_column(session, dim: int, model_id: str) -> bool:
    """Make ``search_embedding_<dim>`` the active vector column of every label.

    Another active dimension means another model: its vector indexes are
    dropped, its vectors cleared and the hashes of other models reset, so the
    artifacts are re-embedded. Returns True when the active column changed.
    """
    previous = active_dim(session)
    column = embedding_column(dim)
    for label in SEARCH_LABELS:
        if column not in _columns(session, label):
            _native(session, f"ALTER TABLE `{label}` ADD {column} FLOAT[{int(dim)}]")
    if previous is None or previous == dim:
        return previous is None
    old_column = embedding_column(previous)
    existing = indexes(session)
    for label in SEARCH_LABELS:
        if vec_index(label, previous) in existing:
            _native(session, f"CALL DROP_VECTOR_INDEX('{label}', '{vec_index(label, previous)}')")
        _native(session, f"MATCH (n:`{label}`) WHERE n.{old_column} IS NOT NULL SET n.{old_column} = NULL")
        _native(session, f"MATCH (n:`{label}`) WHERE n.search_hash IS NOT NULL "
                         "AND NOT n.search_hash STARTS WITH $model SET n.search_hash = NULL",
                model=model_prefix(model_id))
    return True


def ensure_indexes(session, dim: Optional[int] = None) -> None:
    """Create the missing vector indexes of ``dim``."""
    if not dim:
        return
    existing = indexes(session)
    for label in SEARCH_LABELS:
        if vec_index(label, dim) not in existing:
            _native(session, f"CALL CREATE_VECTOR_INDEX('{label}', '{vec_index(label, dim)}', "
                             f"'{embedding_column(dim)}', metric := 'cosine')")


def has_search_texts(session) -> bool:
    """Whether any artifact has a search text (keyword search possible)."""
    for label in SEARCH_LABELS:
        try:
            if _native(session, f"MATCH (n:`{label}`) WHERE n.search_text IS NOT NULL RETURN n.uid LIMIT 1"):
                return True
        except Exception:  # noqa: BLE001 - column not created yet
            continue
    return False


def search_ready(session) -> Dict[str, bool]:
    """Which searches can run: keywords (search texts written) / vectors (an index on every label)."""
    try:
        existing = indexes(session)
    except Exception:
        return {"keyword": False, "vector": False}
    dims = {int(m.group(2)) for m in map(_VEC_INDEX.match, existing) if m}
    return {
        "keyword": has_search_texts(session),
        "vector": any(all(vec_index(label, d) in existing for label in SEARCH_LABELS) for d in dims),
    }


# ---------------------------------------------------------------------------
# Reading artifacts and writing their search data
# ---------------------------------------------------------------------------

def _scope(var: str, prefix: Optional[str], paths: Optional[Sequence[str]]) -> str:
    if paths is not None:
        return f"{var}.path IN $paths"
    if prefix:
        return f"{var}.path STARTS WITH $prefix"
    return "true"


def collect(session, prefix: Optional[str], paths: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Artifacts under ``prefix`` (or in ``paths``) with their current search data."""
    rows: List[Dict[str, Any]] = []
    for label in SEARCH_LABELS:
        rows.extend(
            dict(row, kind=label)
            for row in _native(
                session,
                f"MATCH (n:`{label}`) WHERE {_scope('n', prefix, paths)} "
                "RETURN n.uid AS uid, n.name AS name, n.path AS path, n.line_number AS line, "
                "n.end_line AS end_line, n.source AS source, n.docstring AS docstring, "
                "n.is_dependency AS dep, n.search_text AS search_text, n.search_hash AS search_hash",
                prefix=prefix, paths=list(paths or []),
            )
        )
    return rows


def write_texts(session, label: str, rows: Sequence[Dict[str, Any]]) -> None:
    """rows: ``{uid, text}``."""
    _changed()
    if rows:
        _native(session, f"UNWIND $rows AS row MATCH (n:`{label}` {{uid: row.uid}}) "
                         "SET n.search_text = row.text", rows=list(rows))


def write_vectors(session, label: str, dim: int, rows: Sequence[Dict[str, Any]]) -> None:
    """rows: ``{uid, hash, vec}`` with ``len(vec) == dim``."""
    if rows:
        _native(session, f"UNWIND $rows AS row MATCH (n:`{label}` {{uid: row.uid}}) "
                         f"SET n.{embedding_column(dim)} = row.vec, n.search_hash = row.hash", rows=list(rows))


def clear_text(session, label: str, uids: Iterable[str]) -> None:
    uids = list(uids)
    _changed()
    if uids:
        _native(session, f"MATCH (n:`{label}`) WHERE n.uid IN $uids "
                         "SET n.search_text = NULL, n.search_hash = NULL", uids=uids)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_HIT_FIELDS = ("node.name AS name, node.path AS path, node.line_number AS line, "
               "node.end_line AS end_line, node.source AS source, node.docstring AS docstring")


def vector_candidates(session, vector: Sequence[float], model_id: str, k: int,
                      prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Nearest artifacts of every label, closest first (cosine distance)."""
    dim = len(vector)
    found: List[Dict[str, Any]] = []
    for label in SEARCH_LABELS:
        for row in _native(
            session,
            f"CALL QUERY_VECTOR_INDEX('{label}', '{vec_index(label, dim)}', $vec, $k) "
            "WITH node, distance WHERE node.search_hash STARTS WITH $model "
            f"AND {_scope('node', prefix, None)} "
            f"RETURN {_HIT_FIELDS}, distance ORDER BY distance LIMIT $k",
            vec=list(vector), k=int(k), model=model_prefix(model_id), prefix=prefix,
        ):
            found.append(dict(row, kind=label))
    found.sort(key=lambda r: r["distance"])
    return found[:k]


# Keyword ranking (BM25) -------------------------------------------------------

_SUFFIXES = (("ies", "y"), ("sses", "ss"), ("es", ""), ("s", ""), ("ing", ""), ("ed", ""))


def stem(word: str) -> str:
    """Light English stemming: ``discounts``/``discounted`` -> ``discount``."""
    for suffix, repl in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "s" and word.endswith("ss"):
                return word
            if suffix == "es" and not word.endswith(("ches", "shes", "xes", "zes", "sses")):
                continue
            return word[: len(word) - len(suffix)] + repl
    return word


class _Corpus:
    """Inverted index of the search texts of one database."""

    def __init__(self, rows: Sequence[Dict[str, Any]]):
        self.docs: List[tuple] = []          # (label, uid, path)
        self.lengths: List[int] = []
        self.postings: Dict[str, List[tuple]] = {}
        for row in rows:
            counts = Counter(stem(w) for w in (row["t"] or "").split())
            index = len(self.docs)
            self.docs.append((row["label"], row["uid"], row["path"] or ""))
            self.lengths.append(sum(counts.values()))
            for term, tf in counts.items():
                self.postings.setdefault(term, []).append((index, tf))
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0

    def top(self, terms: Sequence[str], k: int, prefix: Optional[str]) -> List[tuple]:
        n = len(self.docs)
        scores: Dict[int, float] = {}
        for term in dict.fromkeys(stem(t) for t in terms):
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for index, tf in postings:
                norm = BM25_K1 * (1 - BM25_B + BM25_B * self.lengths[index] / (self.avgdl or 1))
                scores[index] = scores.get(index, 0.0) + idf * tf * (BM25_K1 + 1) / (tf + norm)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        out = []
        for index, score in ranked:
            label, uid, path = self.docs[index]
            if prefix and not path.startswith(prefix):
                continue
            out.append((label, uid, score))
            if len(out) >= k:
                break
        return out


_corpus_lock = threading.Lock()
_corpus_cache: Dict[int, tuple] = {}   # id(database) -> (generation, _Corpus)
_generation = 0


def _changed() -> None:
    """Search texts were written: the keyword corpus is rebuilt on next use."""
    global _generation
    with _corpus_lock:
        _generation += 1


def _corpus(session) -> _Corpus:
    conn = getattr(session, "conn", None)
    key = id(getattr(conn, "database", None) or conn or session)
    with _corpus_lock:
        cached = _corpus_cache.get(key)
        generation = _generation
    if cached and cached[0] == generation:
        return cached[1]
    rows: List[Dict[str, Any]] = []
    for label in SEARCH_LABELS:
        for row in _native(session, f"MATCH (n:`{label}`) WHERE n.search_text IS NOT NULL "
                                    "RETURN n.uid AS uid, n.path AS path, n.search_text AS t"):
            rows.append(dict(row, label=label))
    corpus = _Corpus(rows)
    with _corpus_lock:
        _corpus_cache[key] = (generation, corpus)
    return corpus


def keyword_candidates(session, query: str, k: int, prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Best BM25 matches over the search texts, best first.

    Nodes deleted since the corpus was built are not found by the final
    MATCH and drop out of the results.
    """
    ranked = _corpus(session).top(query.split(), int(k), prefix)
    by_label: Dict[str, List[str]] = {}
    for label, uid, _score in ranked:
        by_label.setdefault(label, []).append(uid)
    rows: Dict[tuple, Dict[str, Any]] = {}
    for label, uids in by_label.items():
        for row in _native(session, f"MATCH (node:`{label}`) WHERE node.uid IN $uids "
                                    f"AND node.search_text IS NOT NULL RETURN {_HIT_FIELDS}, node.uid AS uid",
                           uids=uids):
            rows[(label, row.pop("uid"))] = dict(row, kind=label)
    return [dict(rows[(label, uid)], score=score) for label, uid, score in ranked if (label, uid) in rows]


def named(session, names: Sequence[str], prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Searchable artifacts whose name is one of ``names`` (case-insensitive)."""
    if not names:
        return []
    found: List[Dict[str, Any]] = []
    for label in SEARCH_LABELS:
        for row in _native(
            session,
            f"MATCH (node:`{label}`) WHERE lower(node.name) IN $names AND node.search_text IS NOT NULL "
            f"AND {_scope('node', prefix, None)} RETURN {_HIT_FIELDS}",
            names=[n.lower() for n in names], prefix=prefix,
        ):
            found.append(dict(row, kind=label))
    return found


def stats(session, model_id: str, prefix: Optional[str] = None) -> Dict[str, int]:
    """``items`` searchable artifacts, ``embedded`` those with a vector of ``model_id``."""
    total = embedded = 0
    for label in SEARCH_LABELS:
        rows = _native(
            session,
            f"MATCH (n:`{label}`) WHERE n.search_text IS NOT NULL AND {_scope('n', prefix, None)} "
            "RETURN count(n) AS items, "
            "sum(CASE WHEN n.search_hash STARTS WITH $model THEN 1 ELSE 0 END) AS embedded",
            model=model_prefix(model_id), prefix=prefix,
        )
        if rows:
            total += int(rows[0]["items"] or 0)
            embedded += int(rows[0]["embedded"] or 0)
    return {"items": total, "embedded": embedded}


# ---------------------------------------------------------------------------
# Vectors kept across node re-creation
# ---------------------------------------------------------------------------
# Re-indexing a file deletes and recreates its nodes, which drops their
# vectors. Before the deletion, the vectors are kept here by search_hash
# (model + text); the indexer takes them back for the artifacts whose text did
# not change instead of embedding them again. Bounded: entries nobody takes
# back (deleted code) are evicted oldest first.

STASH_MAX = 50_000
_stash: "OrderedDict[str, List[float]]" = OrderedDict()
_stash_lock = threading.Lock()


def stash_vectors(session, paths: Optional[Sequence[str]] = None, prefix: Optional[str] = None) -> int:
    """Keep the vectors of the artifacts in ``paths`` (or under ``prefix``)."""
    dim = active_dim(session)
    if dim is None:
        return 0
    column = embedding_column(dim)
    kept = 0
    for label in SEARCH_LABELS:
        rows = _native(
            session,
            f"MATCH (n:`{label}`) WHERE {_scope('n', prefix, paths)} AND n.search_hash IS NOT NULL "
            f"AND n.{column} IS NOT NULL RETURN n.search_hash AS h, n.{column} AS v",
            prefix=prefix, paths=list(paths or []),
        )
        with _stash_lock:
            for row in rows:
                _stash[row["h"]] = list(row["v"])
                _stash.move_to_end(row["h"])
                kept += 1
            while len(_stash) > STASH_MAX:
                _stash.popitem(last=False)
    return kept


def take_stashed(hashes: Iterable[str]) -> Dict[str, List[float]]:
    """Remove and return the kept vectors of ``hashes``."""
    found: Dict[str, List[float]] = {}
    with _stash_lock:
        for h in hashes:
            vec = _stash.pop(h, None)
            if vec is not None:
                found[h] = vec
    return found


def clear_stash() -> None:
    with _stash_lock:
        _stash.clear()
