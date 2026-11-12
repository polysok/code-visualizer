# src/code_visualizer/tools/indexing/vector_resolver.py
"""Vector-similarity-based call resolution using pre-computed Function embeddings.

Used as a tiebreaker in ``resolve_function_call`` when heuristic tiers cannot
produce a high-confidence answer: the candidates' stored embeddings are scored
with cosine similarity.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...utils.debug_log import warning_logger
from .embeddings import cosine_similarity

_DEFAULT_THRESHOLD = 0.75


class VectorResolver:
    """Pick the best Function among candidates using embedding similarity."""

    def __init__(
        self,
        driver: Any,
        threshold: float = _DEFAULT_THRESHOLD,
    ):
        self.driver = driver
        self.threshold = threshold
        self._embedder = None

    def _get_embedder(self):
        if self._embedder is None:
            from code_visualizer.tools.indexing.embeddings import _get_embedder

            self._embedder = _get_embedder()
        return self._embedder

    def _embed_query(self, text: str) -> List[float]:
        return self._get_embedder().embed_batch([text])[0]

    def _resolve_property_scan(
        self,
        called_name: str,
        candidate_paths: List[str],
        query_vec: List[float],
    ) -> Optional[str]:
        """Brute-force cosine similarity over stored node embeddings."""
        with self.driver.session() as session:
            try:
                result = session.run(
                    """
                    MATCH (f:Function)
                    WHERE f.name = $name
                      AND f.path IN $paths
                      AND f.embedding IS NOT NULL
                    RETURN f.path AS path, f.embedding AS embedding
                    """,
                    name=called_name,
                    paths=candidate_paths,
                )
                best_path: Optional[str] = None
                best_score = self.threshold
                for row in result:
                    embedding = row.get("embedding")
                    if not embedding:
                        continue
                    if hasattr(embedding, "tolist"):
                        embedding = embedding.tolist()
                    score = cosine_similarity(query_vec, list(embedding))
                    if score >= best_score:
                        best_score = score
                        best_path = row["path"]
                return best_path
            except Exception as e:
                warning_logger(f"[VECTOR] Property-scan query failed: {e}")
        return None

    def resolve(
        self,
        called_name: str,
        caller_qualified_name: Optional[str],
        candidate_paths: List[str],
        repo_path: str,
    ) -> Optional[str]:
        """Return the file path of the best-matching Function among candidates."""
        if not candidate_paths:
            return None

        query_text = f"{caller_qualified_name or ''} calls {called_name}"
        try:
            query_vec = self._embed_query(query_text)
        except Exception as e:
            warning_logger(f"[VECTOR] Embed query failed: {e}")
            return None

        return self._resolve_property_scan(called_name, candidate_paths, query_vec)

    def resolve_bulk(
        self,
        calls: List[Dict[str, Any]],
        repo_path: str,
    ) -> Dict[int, str]:
        """Resolve a list of calls; returns {call_index: resolved_path}."""
        results: Dict[int, str] = {}
        for idx, call in enumerate(calls):
            resolved = self.resolve(
                called_name=call["called_name"],
                caller_qualified_name=call.get("caller_qualified_name"),
                candidate_paths=call["candidate_paths"],
                repo_path=repo_path,
            )
            if resolved:
                results[idx] = resolved
        return results
