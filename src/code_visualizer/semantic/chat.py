"""Chat of the graph page: hybrid search, then one LLM call.

1. The hybrid search returns ~15 candidates for the last user message.
2. Without an LLM configured, the first 5 are the reply.
3. With one, a single ``/v1/chat/completions`` call (OpenAI-compatible:
   Ollama, LM Studio, OpenAI, Mistral, vLLM...) picks the relevant candidates
   - by number, so it cannot invent an artifact - and writes a short answer
   only when the request asks a question the code answers.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import settings as ai_settings
from .embedders import api_url, auth_headers
from .search import SearchEngine, engine as default_engine

MAX_RESULTS = 5
CANDIDATES = 15
EXCERPT_CHARS = 700
HISTORY_MESSAGES = 6

SYSTEM_PROMPT = """You help a developer find code in a repository, shown as a dependency graph.
You receive the developer's request and numbered CANDIDATES (artifacts found by a semantic + keyword search: kind, name, file:line, code excerpt).

Reply with ONE JSON object and nothing else:
{"results": [candidate numbers, most relevant first, at most 5], "answer": null or "text"}

Rules:
- "results": only candidates that really match the request. [] if none does.
- "answer": null when the request is to find / show / locate something: the results are the reply.
  Write a short answer (at most 5 sentences) only when the request asks how / why / what something does
  and the candidates' code lets you answer. Refer to artifacts by name. Never invent code or artifacts.
- Write "answer" in the language of the request. Never mention candidate numbers in it."""


class LlmError(RuntimeError):
    pass


def _last_user_message(messages: Sequence[Dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and str(message.get("content", "")).strip():
            return str(message["content"]).strip()
    return ""


def _candidates_block(hits) -> str:
    lines = []
    for n, hit in enumerate(hits, 1):
        item = hit.item
        body = "\n".join(item.text.split("\n")[2:])[:EXCERPT_CHARS]
        lines.append(f"[{n}] {item.kind} {item.name} - {item.path}:{item.line}\n{body}")
    return "\n\n".join(lines)


MAX_REPLY_TOKENS = 1000


# The reply's shape, for servers that constrain the output to a JSON schema
# (OpenAI, LM Studio, Ollama, llama.cpp, vLLM: response_format json_schema).
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {"type": "array", "items": {"type": "integer"}},
        "answer": {"type": ["string", "null"]},
    },
    "required": ["results", "answer"],
    "additionalProperties": False,
}

# Optional request fields, each dropped when a server rejects it:
# no "reasoning" phase (reasoning models such as gemma-4 otherwise think for
# minutes before a 5-line JSON), and a reply constrained to REPLY_SCHEMA
# (small models write invalid JSON, e.g. an extra bracket).
OPTIONAL_FIELDS = {
    "reasoning": {"reasoning_effort": "none"},
    "json": {"response_format": {"type": "json_schema", "json_schema": {
        "name": "search_reply", "strict": True, "schema": REPLY_SCHEMA}}},
}
_ATTEMPTS = (("reasoning", "json"), ("json",), ("reasoning",), ())
# What each server (url, model) accepted last: asked first next time.
_accepted: Dict[Tuple[str, str], Tuple[str, ...]] = {}


def call_llm(llm: ai_settings.LlmSettings, messages: List[Dict[str, str]], timeout: float = 180.0) -> str:
    """One non-streamed completion.

    Asks for no reasoning phase and for a reply in the JSON schema of the
    reply (OPTIONAL_FIELDS); a server that rejects them (HTTP 400 / 422) is
    asked again without, and what it accepted is remembered.
    """
    import requests

    payload: Dict[str, Any] = {"model": llm.model, "messages": messages, "temperature": 0.1,
                               "max_tokens": MAX_REPLY_TOKENS, "stream": False}
    key = (llm.url, llm.model)
    attempts = list(_ATTEMPTS)
    if key in _accepted:
        attempts.remove(_accepted[key])
        attempts.insert(0, _accepted[key])
    response = None
    for n, fields in enumerate(attempts):
        extra: Dict[str, Any] = {}
        for field in fields:
            extra.update(OPTIONAL_FIELDS[field])
        try:
            response = requests.post(
                api_url(llm.url, "chat/completions"),
                json={**payload, **extra},
                headers=auth_headers(llm.api_key),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise LlmError(f"LLM server unreachable ({llm.url}): {exc}") from exc
        if response.status_code < 400:
            _accepted[key] = fields
            break
        if response.status_code not in (400, 422) or n == len(attempts) - 1:
            break
    if response.status_code >= 400:
        raise LlmError(f"LLM server error {response.status_code}: {response.text[:300]}")
    try:
        return response.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"unexpected LLM response: {response.text[:300]}") from exc


def _picks(values, n_candidates: int) -> List[int]:
    picks: List[int] = []
    for value in values or []:
        try:
            k = int(value) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= k < n_candidates and k not in picks:
            picks.append(k)
    return picks[:MAX_RESULTS]


def _answer(value) -> Optional[str]:
    return str(value).strip() if value not in (None, "", "null") else None


def _salvage(text: str, n_candidates: int) -> Optional[Dict[str, Any]]:
    """The fields of a JSON reply that does not parse: small models write
    invalid JSON ("results": [1, 2] ], an extra bracket), or stop before the
    closing quote and brace (the token limit)."""
    results = re.search(r'"results"\s*:\s*\[([^\]]*)\]?', text)
    answer = re.search(r'"answer"\s*:\s*(null|"((?:[^"\\]|\\.)*)("?))', text, flags=re.S)
    if not results and not answer:
        return None
    picks = _picks(re.findall(r"-?\d+", results.group(1)), n_candidates) if results else []
    text_answer = None
    if answer and answer.group(1) != "null":
        raw = answer.group(2)
        try:
            text_answer = json.loads(f'"{raw}"')
        except ValueError:
            text_answer = raw.replace('\\"', '"').replace("\\n", "\n")
        text_answer = _answer(text_answer)
        if text_answer and not answer.group(3):  # no closing quote: cut short
            text_answer = text_answer.rstrip() + "…"
    return {"results": picks, "answer": text_answer, "parsed": True}


def parse_reply(text: str, n_candidates: int) -> Dict[str, Any]:
    """``{"results": [0-based indexes], "answer": str|None, "parsed": bool}``.

    The JSON may come in a ```json block, followed by text, invalid or cut
    short: its fields are read all the same; only a reply that is not JSON at
    all is shown as text.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```\s*$", "", cleaned).strip()
    start = cleaned.find("{")
    if start >= 0:
        try:
            data, _ = json.JSONDecoder().raw_decode(cleaned[start:])
            if isinstance(data, dict):
                return {"results": _picks(data.get("results"), n_candidates),
                        "answer": _answer(data.get("answer")), "parsed": True}
        except ValueError:
            pass
        salvaged = _salvage(cleaned[start:], n_candidates)
        if salvaged:
            return salvaged
    # Not JSON: keep the search order, show the text as the answer.
    return {"results": None, "answer": cleaned or None, "parsed": False}


def list_models(llm: ai_settings.LlmSettings, timeout: float = 10.0) -> List[str]:
    """Model ids served at ``llm.url`` (/v1/models, else Ollama's /api/tags)."""
    import requests

    errors = []
    for url, extract in (
        (api_url(llm.url, "models"), lambda d: [m.get("id") for m in d.get("data", [])]),
        (llm.url.rstrip("/").removesuffix("/v1") + "/api/tags", lambda d: [m.get("name") for m in d.get("models", [])]),
    ):
        try:
            response = requests.get(url, headers=auth_headers(llm.api_key), timeout=timeout)
            if response.status_code < 400:
                return sorted(m for m in extract(response.json()) if m)
            errors.append(f"{url}: HTTP {response.status_code}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    raise LlmError("; ".join(errors))


def chat(messages: Sequence[Dict[str, str]], repos: Optional[Sequence[str]] = None,
         search_engine: Optional[SearchEngine] = None,
         llm: Optional[ai_settings.LlmSettings] = None) -> Dict[str, Any]:
    """Reply to the conversation: ``{"hits": [...], "answer", "mode", "warning", ...}``."""
    question = _last_user_message(messages)
    search_engine = search_engine or default_engine()
    llm = llm or ai_settings.llm_settings()
    found = search_engine.search(question, limit=CANDIDATES, repos=repos)
    candidates = found["hits"]
    reply: Dict[str, Any] = {
        "question": question, "hits": candidates[:MAX_RESULTS], "answer": None,
        "search_mode": found["mode"], "warning": found.get("warning"), "llm": None, "reranked": False,
        # The plain search top 5, shown as "not retained" when the LLM keeps none.
        "search_hits": candidates[:MAX_RESULTS],
    }
    if not question or not candidates or not llm.configured:
        return reply

    history = [
        {"role": m["role"], "content": str(m.get("content", ""))[:2000]}
        for m in list(messages)[:-1][-HISTORY_MESSAGES:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    prompt = [{"role": "system", "content": SYSTEM_PROMPT}, *history,
              {"role": "user", "content": f"REQUEST:\n{question}\n\nCANDIDATES:\n{_candidates_block(candidates)}"}]
    reply["llm"] = llm.model
    try:
        parsed = parse_reply(call_llm(llm, prompt), len(candidates))
    except LlmError as exc:
        reply["warning"] = f"{exc} - showing the search results only"
        return reply
    reply["answer"] = parsed["answer"]
    if parsed["results"] is not None:
        reply["reranked"] = True
        reply["hits"] = [candidates[k] for k in parsed["results"]]
    return reply
