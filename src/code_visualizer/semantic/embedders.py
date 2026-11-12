"""Embedders for the natural-language search.

* ``local``  - fastembed (ONNX, no torch). Default model: multilingual-e5-small,
  so a question in French finds code written in English. Downloaded once
  (~130 MB) into ``~/.code-visualizer/models``.
* ``openai`` - any server exposing the OpenAI ``/v1/embeddings`` route
  (LM Studio, Ollama, OpenAI, Mistral, vLLM...).

Vectors are L2-normalised, so cosine similarity is a dot product.
"""
from __future__ import annotations

import math
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .settings import DEFAULT_LOCAL_MODEL, EmbedSettings


class EmbeddingError(RuntimeError):
    """The embedder cannot run (missing package, server error...)."""


def _prefixes(model: str) -> tuple:
    """(query prefix, document prefix) expected by some model families."""
    m = model.lower()
    if "e5" in m:
        return "query: ", "passage: "
    if "nomic" in m:
        return "search_query: ", "search_document: "
    if "bge" in m and "m3" not in m:
        return "Represent this sentence for searching relevant passages: ", ""
    return "", ""


def _normalise(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm == 0.0:
        return [0.0 for _ in vec]
    return [float(x) / norm for x in vec]


class Embedder:
    model_id: str = ""
    batch_size: int = 32

    def __init__(self, settings: EmbedSettings):
        self.settings = settings
        self.model_id = settings.model_id
        self._query_prefix, self._doc_prefix = _prefixes(settings.model)

    def _embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [self._doc_prefix + t for t in texts[i:i + self.batch_size]]
            out.extend(_normalise(v) for v in self._embed(batch))
        return out

    def embed_query(self, text: str) -> List[float]:
        return _normalise(self._embed([self._query_prefix + text])[0])


# ---------------------------------------------------------------------------
# Local (fastembed)
# ---------------------------------------------------------------------------

_LOCAL_MODELS: Dict[str, object] = {}
_LOCAL_LOCK = threading.Lock()


def models_dir() -> Path:
    custom = os.environ.get("CVIZ_MODELS_DIR")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".code-visualizer" / "models"


def _register_default_model(text_embedding_cls) -> None:
    """multilingual-e5-small is not in fastembed's catalogue: declare it."""
    supported = {m["model"] for m in text_embedding_cls.list_supported_models()}
    if DEFAULT_LOCAL_MODEL in supported:
        return
    from fastembed.common.model_description import ModelSource, PoolingType

    text_embedding_cls.add_custom_model(
        model=DEFAULT_LOCAL_MODEL,
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf=DEFAULT_LOCAL_MODEL),
        dim=384,
        model_file="onnx/model_quantized.onnx",
        description="multilingual-e5-small (quantized ONNX), registered by Code Visualizer",
    )


class LocalEmbedder(Embedder):
    batch_size = 16

    def __init__(self, settings: EmbedSettings):
        super().__init__(settings)
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbeddingError(
                "local embeddings need the 'fastembed' package: "
                "uv tool install --force 'code-visualizer[semantic]' "
                "(or choose an OpenAI-compatible embedding server in Settings > AI)"
            ) from exc
        with _LOCAL_LOCK:
            model = _LOCAL_MODELS.get(settings.model)
            if model is None:
                if settings.model == DEFAULT_LOCAL_MODEL:
                    _register_default_model(TextEmbedding)
                try:
                    model = TextEmbedding(settings.model, cache_dir=str(models_dir()))
                except Exception as exc:  # noqa: BLE001 - unknown model, download failure
                    raise EmbeddingError(f"cannot load local model '{settings.model}': {exc}") from exc
                _LOCAL_MODELS[settings.model] = model
        self._model = model

    def _embed(self, texts: List[str]) -> List[List[float]]:
        return [v.tolist() for v in self._model.embed(texts)]


# ---------------------------------------------------------------------------
# OpenAI-compatible server
# ---------------------------------------------------------------------------


def api_url(base: str, route: str) -> str:
    """``base`` with or without a trailing ``/v1`` -> ``.../v1/<route>``."""
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return f"{base}/{route.lstrip('/')}"


def auth_headers(api_key: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class OpenAICompatibleEmbedder(Embedder):
    batch_size = 32

    def __init__(self, settings: EmbedSettings, timeout: float = 120.0):
        super().__init__(settings)
        if not settings.url:
            raise EmbeddingError("no embedding server URL configured (Settings > AI)")
        if not settings.model:
            raise EmbeddingError("no embedding model configured (Settings > AI)")
        self.timeout = timeout

    def _embed(self, texts: List[str]) -> List[List[float]]:
        import requests

        try:
            response = requests.post(
                api_url(self.settings.url, "embeddings"),
                json={"model": self.settings.model, "input": texts},
                headers=auth_headers(self.settings.api_key),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise EmbeddingError(f"embedding server unreachable ({self.settings.url}): {exc}") from exc
        if response.status_code >= 400:
            raise EmbeddingError(f"embedding server error {response.status_code}: {response.text[:300]}")
        try:
            data = sorted(response.json()["data"], key=lambda d: d.get("index", 0))
            vectors = [d["embedding"] for d in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"unexpected embedding response: {response.text[:300]}") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError(f"embedding server returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors


def get_embedder(settings: EmbedSettings) -> Embedder:
    if settings.provider == "openai":
        return OpenAICompatibleEmbedder(settings)
    return LocalEmbedder(settings)


def probe(settings: EmbedSettings) -> Optional[str]:
    """None when the embedder can probably run, else why not (cheap check)."""
    if settings.provider == "openai":
        if not settings.url:
            return "no embedding server URL configured"
        if not settings.model:
            return "no embedding model configured"
        return None
    import importlib.util

    if importlib.util.find_spec("fastembed") is None:
        return "the 'fastembed' package is not installed (pip install 'code-visualizer[semantic]')"
    return None
