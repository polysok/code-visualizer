"""semantic/chat.py and the /api/chat, /api/ai/* routes."""
import json

import pytest
from fastapi.testclient import TestClient

from code_visualizer.semantic import chat as chat_mod
from code_visualizer.semantic import settings as ai_settings
from code_visualizer.semantic.search import Hit, Item

LLM = ai_settings.LlmSettings("lmstudio", "http://llm", "gemma", "")


def _hit(n):
    item = Item(f"Function|/r/f{n}.py|f{n}|1", "Function", f"f{n}", f"/r/f{n}.py", 1, 3,
                f"Function f{n}\nf{n}.py\ndef f{n}():\n    return {n}")
    return Hit(item, 1.0 / n, 0.8, None)


class FakeEngine:
    def __init__(self, n=8):
        self.hits = [_hit(i) for i in range(1, n + 1)]
        self.calls = []

    def search(self, query, limit=5, repos=None):
        self.calls.append((query, limit, repos))
        return {"hits": self.hits[:limit], "mode": "hybrid", "warning": None}


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def fake_llm(monkeypatch, content=None, statuses=(200,)):
    sent = []
    statuses = list(statuses)

    def post(url, json, headers, timeout):
        sent.append({"url": url, **json})
        status = statuses.pop(0) if statuses else 200
        if status != 200:
            return Resp(status, {"error": "bad param"})
        return Resp(200, {"choices": [{"message": {"content": content}}]})

    import requests
    monkeypatch.setattr(requests, "post", post)
    return sent


def test_parse_reply_keeps_valid_candidates_only():
    r = chat_mod.parse_reply('<think>hm</think>```json\n{"results": [3, "1", 3, 99, "x"], "answer": null}\n```', 5)
    assert r == {"results": [2, 0], "answer": None, "parsed": True}
    r = chat_mod.parse_reply("Sorry, plain text.", 5)
    assert r["results"] is None and r["answer"] == "Sorry, plain text."


def test_without_llm_returns_search_top5():
    eng = FakeEngine()
    r = chat_mod.chat([{"role": "user", "content": "where is f2"}], search_engine=eng,
                      llm=ai_settings.LlmSettings("lmstudio", "http://llm", "", ""))
    assert [h.item.name for h in r["hits"]] == ["f1", "f2", "f3", "f4", "f5"]
    assert r["llm"] is None and not r["reranked"] and eng.calls[0][1] == chat_mod.CANDIDATES


def test_llm_reranks_answers_and_gets_history(monkeypatch):
    sent = fake_llm(monkeypatch, '{"results": [4, 2], "answer": "f4 returns 4."}')
    msgs = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "what does f4 return?"}]
    r = chat_mod.chat(msgs, search_engine=FakeEngine(), llm=LLM)
    assert [h.item.name for h in r["hits"]] == ["f4", "f2"] and r["answer"] == "f4 returns 4."
    req = sent[0]
    assert req["url"] == "http://llm/v1/chat/completions" and req["reasoning_effort"] == "none"
    assert [m["role"] for m in req["messages"]] == ["system", "user", "assistant", "user"]
    assert "[8] Function f8 - /r/f8.py:1" in req["messages"][-1]["content"]


def test_llm_rejecting_reasoning_param_is_retried_without_it(monkeypatch):
    sent = fake_llm(monkeypatch, '{"results": [], "answer": null}', statuses=(400, 200))
    r = chat_mod.chat([{"role": "user", "content": "x"}], search_engine=FakeEngine(), llm=LLM)
    assert "reasoning_effort" in sent[0] and "reasoning_effort" not in sent[1]
    assert r["hits"] == [] and r["reranked"] and len(r["search_hits"]) == 5


def test_llm_down_falls_back_to_search(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "post", boom)
    r = chat_mod.chat([{"role": "user", "content": "x"}], search_engine=FakeEngine(), llm=LLM)
    assert len(r["hits"]) == 5 and "unreachable" in r["warning"]


@pytest.fixture
def client(monkeypatch):
    from code_visualizer.viz import server

    eng = FakeEngine()
    monkeypatch.setattr(chat_mod, "default_engine", lambda: eng)
    monkeypatch.setattr(ai_settings, "llm_settings", lambda: LLM)
    return TestClient(server.app)


def test_api_chat(client, monkeypatch):
    fake_llm(monkeypatch, '{"results": [1], "answer": null}')
    body = client.post("/api/chat", json={"messages": [{"role": "user", "content": "f1?"}]}).json()
    assert [h["name"] for h in body["hits"]] == ["f1"] and body["hits"][0]["path"] == "/r/f1.py"
    bad = client.post("/api/chat", json={"messages": [{"role": "system", "content": "x"}]})
    assert bad.status_code == 422


def test_ai_write_routes_are_local_json_only(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(ai_settings, "save", lambda values: saved.update(values))
    ok = client.post("/api/ai/config", json={"CHAT_LLM_MODEL": "gemma"})
    assert ok.status_code == 200 and saved == {"CHAT_LLM_MODEL": "gemma"}
    foreign = client.post("/api/ai/config", json={"CHAT_LLM_MODEL": "x"}, headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403
    form = client.post("/api/ai/config", content="CHAT_LLM_MODEL=x",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert form.status_code in (415, 422) and saved == {"CHAT_LLM_MODEL": "gemma"}  # refused, nothing saved


def test_api_models(client, monkeypatch):
    import requests

    def get(url, headers, timeout):
        if url.endswith("/v1/models"):
            return Resp(404, {})
        return Resp(200, {"models": [{"name": "llama3"}, {"name": "gemma"}]})
    monkeypatch.setattr(requests, "get", get)
    body = client.post("/api/ai/models", json={"url": "http://127.0.0.1:11434"}).json()
    assert body == {"models": ["gemma", "llama3"]}
