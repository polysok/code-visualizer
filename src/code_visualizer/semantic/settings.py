"""Settings of the natural-language search, stored in the CViz config file.

They are edited from the graph page (Settings > AI) through ``/api/ai/config``
and read by ``cviz index``, ``cviz watch``, ``cviz embed`` and the page server:
the vectors are computed at indexing time, without a browser.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

# Config key -> default. Registered in cli.config_manager.DEFAULT_CONFIG.
DEFAULTS: Dict[str, str] = {
    "SEMANTIC_SEARCH": "false",
    "SEARCH_EMBED_PROVIDER": "local",
    "SEARCH_EMBED_MODEL": "",
    "SEARCH_EMBED_URL": "http://127.0.0.1:1234",
    "SEARCH_EMBED_API_KEY": "",
    "CHAT_LLM_PROVIDER": "lmstudio",
    "CHAT_LLM_URL": "http://127.0.0.1:1234",
    "CHAT_LLM_MODEL": "",
    "CHAT_LLM_API_KEY": "",
}

DESCRIPTIONS: Dict[str, str] = {
    "SEMANTIC_SEARCH": "Vectorize code artifacts for the natural-language search of the graph page (cviz graph start-up, watcher, cviz index --embed)",
    "SEARCH_EMBED_PROVIDER": "Embeddings for the natural-language search: 'local' (fastembed, nothing leaves the machine) or 'openai' (any OpenAI-compatible /v1/embeddings server)",
    "SEARCH_EMBED_MODEL": "Embedding model (empty = Xenova/multilingual-e5-small for 'local'; required for 'openai')",
    "SEARCH_EMBED_URL": "Base URL of the OpenAI-compatible embedding server (e.g. http://127.0.0.1:1234 for LM Studio, http://127.0.0.1:11434 for Ollama)",
    "SEARCH_EMBED_API_KEY": "API key of the embedding server (empty for local servers)",
    "CHAT_LLM_PROVIDER": "LLM of the graph page chat: ollama, lmstudio or other (OpenAI-compatible /v1/chat/completions)",
    "CHAT_LLM_URL": "Base URL of the chat LLM server",
    "CHAT_LLM_MODEL": "Chat LLM model (empty = no LLM: the chat returns the search results only)",
    "CHAT_LLM_API_KEY": "API key of the chat LLM server (empty for local servers)",
}

VALIDATORS: Dict[str, list] = {
    "SEMANTIC_SEARCH": ["true", "false"],
    "SEARCH_EMBED_PROVIDER": ["local", "openai"],
    "CHAT_LLM_PROVIDER": ["ollama", "lmstudio", "other"],
}

SECRET_KEYS = ("SEARCH_EMBED_API_KEY", "CHAT_LLM_API_KEY")

DEFAULT_LOCAL_MODEL = "Xenova/multilingual-e5-small"


def _get(key: str) -> str:
    try:
        from ..cli.config_manager import get_config_value

        value = get_config_value(key)
    except Exception:  # noqa: BLE001 - config unreadable: fall back to defaults
        value = None
    if value is None:
        return DEFAULTS[key]
    return str(value).strip().strip("'\"")


def enabled() -> bool:
    return _get("SEMANTIC_SEARCH").lower() == "true"


@dataclass(frozen=True)
class EmbedSettings:
    provider: str  # local | openai
    model: str
    url: str
    api_key: str

    @property
    def model_id(self) -> str:
        """Identifies the vector space: vectors of different ids never mix."""
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class LlmSettings:
    provider: str
    url: str
    model: str
    api_key: str

    @property
    def configured(self) -> bool:
        return bool(self.url and self.model)


def embed_settings() -> EmbedSettings:
    provider = _get("SEARCH_EMBED_PROVIDER").lower() or "local"
    model = _get("SEARCH_EMBED_MODEL")
    if provider == "local" and not model:
        model = DEFAULT_LOCAL_MODEL
    return EmbedSettings(provider, model, _get("SEARCH_EMBED_URL").rstrip("/"), _get("SEARCH_EMBED_API_KEY"))


def llm_settings() -> LlmSettings:
    return LlmSettings(
        _get("CHAT_LLM_PROVIDER").lower(),
        _get("CHAT_LLM_URL").rstrip("/"),
        _get("CHAT_LLM_MODEL"),
        _get("CHAT_LLM_API_KEY"),
    )


def public_config() -> Dict[str, object]:
    """Settings for the page: secrets are replaced by whether they are set."""
    out: Dict[str, object] = {}
    for key in DEFAULTS:
        value = _get(key)
        if key in SECRET_KEYS:
            out[key] = ""
            out[key + "_SET"] = bool(value)
        else:
            out[key] = value
    return out


def save(values: Dict[str, Optional[str]]) -> Dict[str, str]:
    """Persist the given keys (unknown keys ignored).

    A secret sent as ``None`` is left unchanged; an empty string clears it.
    Returns the values written. Raises ``ValueError`` on an invalid value.
    """
    from ..cli.config_manager import load_config, save_config, ensure_config_dir

    written: Dict[str, str] = {}
    for key, value in values.items():
        if key not in DEFAULTS or value is None:
            continue
        value = str(value).strip()
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key}: line breaks are not allowed")
        allowed = VALIDATORS.get(key)
        if allowed and value.lower() not in allowed:
            raise ValueError(f"{key} must be one of: {', '.join(allowed)}")
        written[key] = value.lower() if allowed else value
    if written:
        ensure_config_dir()
        config = load_config()
        config.update(written)
        save_config(config)
    return written
