"""semantic/search.py and GET /api/search, on a real LadybugDB (BM25 keywords, vector index)."""
import pytest
from fastapi.testclient import TestClient

from code_visualizer.semantic import graph_index, indexer
from code_visualizer.semantic import search as search_mod
from code_visualizer.semantic.settings import EmbedSettings

pytest.importorskip("ladybug")

EMBED = EmbedSettings("openai", "fake", "http://fake", "")

# 3-d "meaning" space: x = money, y = rendering, z = files.
DOCS = [
    ("compute_discount", "/r/pricing.py", "def compute_discount(order):\n    return order.total * rate", [1, 0, 0]),
    ("InvoicePdfExporter", "/r/export.py", "class InvoicePdfExporter:\n    def export(self): ...", [0.7, 0.7, 0]),
    ("render_navbar", "/r/ui.py", "def render_navbar(user):\n    return html", [0, 1, 0]),
    ("read_source", "/r/io.py", "def read_source(path):\n    return open(path).read()", [0, 0, 1]),
    ("stream", "/other/s.py", "def stream():\n    yield 1", [0, 0.2, 0.9]),
]
QUERIES = {"remise client": [0.95, 0.05, 0], "lire un fichier": [0, 0, 1], "SSE events stream": [0, 0.1, 1]}


@pytest.fixture
def driver(tmp_path):
    from code_visualizer.core import ladybugdb_unavailable_reason
    from code_visualizer.core.database_ladybug import LadybugDBManager

    reason = ladybugdb_unavailable_reason()
    if reason:
        pytest.skip(reason)
    manager = LadybugDBManager(str(tmp_path / "db"))
    drv = manager.get_driver()
    with drv.session() as s:
        for i, (name, path, src, _v) in enumerate(DOCS):
            s.run("MERGE (f:Function {uid: $uid}) SET f.name = $name, f.path = $path, "
                  "f.line_number = 1, f.end_line = 2, f.source = $src",
                  uid=f"u{i}", name=name, path=path, src=src)
    try:
        with drv.session() as s:
            graph_index.ensure_extensions(s)
            graph_index.ensure_text_columns(s)
            for repo in ("/r", "/other"):
                arts = indexer.to_artifacts(graph_index.collect(s, repo + "/"), repo)
                indexer.store_keywords(s, arts)
                graph_index.ensure_embedding_column(s, 3, EMBED.model_id)
                vecs = {name: v for name, _p, _s, v in DOCS}
                graph_index.write_vectors(s, "Function", 3, [
                    {"uid": a.uid, "hash": indexer.text_hash(EMBED.model_id, a.text),
                     "vec": [float(x) for x in vecs[a.name]]} for a in arts])
            graph_index.ensure_indexes(s, 3)
    except graph_index.SearchIndexError as exc:
        manager.close_driver()
        pytest.skip(str(exc))
    yield drv
    manager.close_driver()


@pytest.fixture
def engine(driver, monkeypatch):
    class FakeEmbedder:
        def embed_query(self, text):
            return QUERIES.get(text, [0.0, 0.0, 0.0])

    eng = search_mod.SearchEngine(driver.session)
    monkeypatch.setattr(eng, "_get_embedder", lambda _s: FakeEmbedder())
    return eng


def names(result):
    return [h.item.name for h in result["hits"]]


def test_tokenize_splits_identifiers_and_drops_stopwords():
    assert search_mod.tokenize("computeDiscount compute_discount HTTPServer") == \
        ["compute", "discount", "compute", "discount", "http", "server"]
    assert search_mod.tokenize("Où est la clé API ?") == ["cle", "api"]


def test_code_identifiers_keeps_code_like_words_only():
    assert search_mod.code_identifiers("where is render_navbar or EventStream, the stream") == \
        ["render_navbar", "EventStream"]
    assert search_mod.code_identifiers("the `stream` function") == ["stream"]


def test_fuse_rewards_agreement():
    fused = search_mod.fuse([["a", "b"], ["b", "c"]])
    assert max(fused, key=fused.get) == "b"


def test_semantic_finds_french_question_and_limit(engine):
    r = engine.search("remise client", limit=2, embed_settings=EMBED)
    assert r["mode"] == "hybrid" and names(r) == ["compute_discount", "InvoicePdfExporter"]
    assert r["indexed"] == 5 and r["vectorized"] == 5


def test_keyword_and_semantic_combine(engine):
    r = engine.search("lire un fichier", embed_settings=EMBED)
    assert names(r)[0] == "read_source"


def test_keyword_ranking_comes_from_bm25(engine):
    r = engine.search("compute discount", embed_settings=EMBED)
    assert names(r)[0] == "compute_discount" and r["hits"][0].keyword is not None


def test_exact_identifier_ranks_first_but_plain_words_do_not(engine):
    r = engine.search("where is render_navbar used", embed_settings=EMBED)
    assert names(r)[0] == "render_navbar" and r["hits"][0].exact
    r = engine.search("SSE events stream", embed_settings=EMBED)
    assert not any(h.exact for h in r["hits"])
    r = engine.search("the `stream` function", embed_settings=EMBED)
    assert r["hits"][0].item.name == "stream" and r["hits"][0].exact


def test_repo_filter(engine):
    r = engine.search("SSE events stream", repos=["/r"], embed_settings=EMBED)
    assert names(r) and "stream" not in names(r)


def test_keyword_only_for_a_model_without_vectors(engine):
    other = EmbedSettings("openai", "never-run", "http://fake", "")
    r = engine.search("compute discount", embed_settings=other)
    assert r["mode"] == "keyword" and "no vectors" in r["warning"]
    assert names(r)[0] == "compute_discount"


def test_indexes_follow_deleted_nodes(engine, driver):
    with driver.session() as s:
        s.run("MATCH (f:Function {uid: 'u0'}) DETACH DELETE f")
    r = engine.search("compute discount remise", embed_settings=EMBED)
    assert "compute_discount" not in names(r)


def test_api_search(engine, monkeypatch):
    from code_visualizer.viz import ai_api, server

    monkeypatch.setattr(ai_api, "engine", lambda: engine)
    monkeypatch.setattr(ai_api.ai_settings, "embed_settings", lambda: EMBED)
    monkeypatch.setattr(search_mod.ai_settings, "embed_settings", lambda: EMBED)
    monkeypatch.setenv("CVIZ_PATH_MAP", "/r=/home/me/r")
    client = TestClient(server.app)
    body = client.get("/api/search", params={"q": "remise client", "limit": 1}).json()
    (hit,) = body["hits"]
    assert hit["name"] == "compute_discount" and hit["path"] == "/home/me/r/pricing.py"
    assert hit["indexed_path"] == "/r/pricing.py" and hit["id"] == "Function|/r/pricing.py|compute_discount|1"
    assert "order.total" in hit["snippet"]
    assert client.get("/api/search", params={"q": "x", "limit": 50}).status_code == 422


def test_repos_to_vectorize_lists_repos_without_current_vectors(driver, monkeypatch):
    """cviz graph vectorizes at start-up the repositories the index left out."""
    from code_visualizer.viz import ai_api

    monkeypatch.setattr(ai_api, "_session", driver.session)
    monkeypatch.setattr(ai_api, "indexed_repo_roots", lambda max_age=0: ["/other", "/r", "/unvectorized"])
    monkeypatch.setattr(ai_api.ai_settings, "embed_settings", lambda: EMBED)
    assert ai_api.repos_to_vectorize() == ["/unvectorized"]

    other_model = EmbedSettings("openai", "another-model", "http://fake", "")
    monkeypatch.setattr(ai_api.ai_settings, "embed_settings", lambda: other_model)
    assert ai_api.repos_to_vectorize() == ["/other", "/r", "/unvectorized"]


def test_catch_up_vectorization_starts_a_background_job(monkeypatch):
    import threading
    from code_visualizer.viz import ai_api

    started = threading.Event()
    calls = []
    monkeypatch.setattr(ai_api.ai_settings, "enabled", lambda: True)
    monkeypatch.setattr(ai_api.ai_settings, "embed_settings", lambda: EMBED)
    monkeypatch.setattr(ai_api, "probe", lambda _s: None)
    monkeypatch.setattr(ai_api, "repos_to_vectorize", lambda: ["/r"])
    monkeypatch.setattr(ai_api, "_start_embed", lambda repos: (calls.append(repos), started.set()))
    ai_api.catch_up_vectorization(delay=0)
    assert started.wait(5) and calls == [["/r"]]

    # Search off: nothing starts.
    started.clear()
    monkeypatch.setattr(ai_api.ai_settings, "enabled", lambda: False)
    ai_api.catch_up_vectorization(delay=0)
    assert not started.wait(0.5)


def test_stem_and_bm25_prefix():
    assert [graph_index.stem(w) for w in ("discounts", "discounted", "classes", "class", "boxes", "queries")] == \
        ["discount", "discount", "class", "class", "box", "query"]
    corpus = graph_index._Corpus([
        {"label": "Function", "uid": "a", "path": "/r/a.py", "t": "discount discount order"},
        {"label": "Function", "uid": "b", "path": "/r/b.py", "t": "order total"},
        {"label": "Function", "uid": "c", "path": "/o/c.py", "t": "discounts"},
    ])
    assert {uid for _l, uid, _s in corpus.top(["discount"], 5, None)} == {"a", "c"}  # stemmed match
    assert [uid for _l, uid, _s in corpus.top(["discount"], 5, "/r/")] == ["a"]
    assert corpus.top(["absent"], 5, None) == []


def test_keyword_corpus_follows_text_writes(driver):
    with driver.session() as s:
        assert [r["name"] for r in graph_index.keyword_candidates(s, "navbar", 5)] == ["render_navbar"]
        graph_index.write_texts(s, "Function", [{"uid": "u2", "text": "render navbar zebra"}])
        assert [r["name"] for r in graph_index.keyword_candidates(s, "zebra", 5)] == ["render_navbar"]
        s.run_native("MATCH (f:Function {uid: 'u2'}) DETACH DELETE f")
        assert graph_index.keyword_candidates(s, "zebra", 5) == []  # deleted node drops out
