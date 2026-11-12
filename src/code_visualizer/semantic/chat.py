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
from typing import Any, Dict, List, Optional, Sequence

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
- Write "answer" in the language of the request."""


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


def call_llm(llm: ai_settings.LlmSettings, messages: List[Dict[str, str]], timeout: float = 180.0) -> str:
    """One non-streamed completion.

    Asks for no "reasoning" phase (``reasoning_effort: none``): reasoning
    models such as gemma-4 otherwise think for minutes before a 5-line JSON.
    A server that rejects the parameter is asked again without it.
    """
    import requests

    payload: Dict[str, Any] = {"model": llm.model, "messages": messages, "temperature": 0.1,
                               "max_tokens": MAX_REPLY_TOKENS, "stream": False}
    response = None
    for extra in ({"reasoning_effort": "none"}, {}):
        try:
            response = requests.post(
                api_url(llm.url, "chat/completions"),
                json={**payload, **extra},
                headers=auth_headers(llm.api_key),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise LlmError(f"LLM server unreachable ({llm.url}): {exc}") from exc
        if not (extra and response.status_code == 400):
            break
    if response.status_code >= 400:
        raise LlmError(f"LLM server error {response.status_code}: {response.text[:300]}")
    try:
        return response.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"unexpected LLM response: {response.text[:300]}") from exc


def parse_reply(text: str, n_candidates: int) -> Dict[str, Any]:
    """``{"results": [0-based indexes], "answer": str|None, "parsed": bool}``."""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            picks: List[int] = []
            for value in data.get("results") or []:
                try:
                    k = int(value) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= k < n_candidates and k not in picks:
                    picks.append(k)
            answer = data.get("answer")
            answer = str(answer).strip() if answer not in (None, "", "null") else None
            return {"results": picks[:MAX_RESULTS], "answer": answer, "parsed": True}
        except (ValueError, AttributeError):
            pass
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
