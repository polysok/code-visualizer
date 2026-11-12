"""semantic/: settings, embedder helpers, text building and the graph sync."""
import pytest

from code_visualizer.semantic import embedders, indexer, settings
from code_visualizer.semantic import graph_index


class FakeEmbedder:
    """Deterministic vectors of ``dim`` from the text length; counts calls."""

    def __init__(self, dim=3):
        self.dim, self.texts = dim, []

    def embed_documents(self, texts):
        self.texts.extend(texts)
        return [[float(len(t) % 7 + 1)] + [0.5] * (self.dim - 1) for t in texts]


@pytest.fixture
def graph(tmp_path, monkeypatch):
    from code_visualizer.core import ladybugdb_unavailable_reason
    from code_visualizer.core.database_ladybug import LadybugDBManager

    pytest.importorskip("ladybug")
    reason = ladybugdb_unavailable_reason()
    if reason:
        pytest.skip(reason)
    manager = LadybugDBManager(str(tmp_path / "db"))
    drv = manager.get_driver()
    with drv.session() as s:
        for uid, name, path in (("a", "computeDiscount", "/r/a.py"), ("b", "sendEmail", "/r/b.py"),
                                ("x", "outside", "/other/x.py")):
            s.run("MERGE (f:Function {uid: $uid}) SET f.name = $name, f.path = $path, f.line_number = 1, "
                  "f.source = $src", uid=uid, name=name, path=path, src=f"def {name}(): pass")
        s.run("MERGE (c:Class {uid: 'c'}) SET c.name = 'Invoice', c.path = '/r/a.py', c.line_number = 5")
    monkeypatch.setattr(indexer, "probe", lambda _s: None)
    yield drv
    manager.close_driver()


def _sync(drv, embedder, monkeypatch, model="m1", **kw):
    monkeypatch.setattr(indexer, "get_embedder", lambda _s: embedder)
    try:
        return indexer.sync(drv.session, "/r", embed_settings=settings.EmbedSettings("openai", model, "http://x", ""),
                            publish=False, **kw)
    except graph_index.SearchIndexError as exc:
        pytest.skip(str(exc))


def test_sync_writes_search_data_into_the_graph_and_embeds_only_changes(graph, monkeypatch):
    emb = FakeEmbedder()
    result = _sync(graph, emb, monkeypatch)
    if result["error"] and "extension" in result["error"]:
        pytest.skip(result["error"])
    assert result == {"repo": "/r", "items": 3, "embedded": 3, "error": None}
    with graph.session() as s:
        assert graph_index.search_ready(s) == {"keyword": True, "vector": True}
        assert graph_index.stats(s, "openai:m1") == {"items": 3, "embedded": 3}
        text = s.run_native("MATCH (f:Function {uid: 'a'}) RETURN f.search_text AS t").data()[0]["t"]
    assert text == "compute discount compute discount compute discount py def compute discount pass"

    # Nothing changed: nothing re-embedded.
    assert _sync(graph, emb, monkeypatch)["embedded"] == 0
    # One source changed: one artifact re-embedded (paths scope).
    with graph.session() as s:
        s.run("MATCH (f:Function {uid: 'b'}) SET f.source = 'def sendEmail(to): smtp.send(to)'")
    assert _sync(graph, emb, monkeypatch, paths=["/r/b.py"])["embedded"] == 1

    # Another model with another dimension: everything re-embedded, old vectors deactivated.
    with graph.session() as s:
        s.run_native("CHECKPOINT")
    assert _sync(graph, FakeEmbedder(dim=4), monkeypatch, model="m2")["embedded"] == 3
    with graph.session() as s:
        assert graph_index.active_dim(s) == 4
        assert graph_index.stats(s, "openai:m1")["embedded"] == 0
        hits = graph_index.vector_candidates(s, [4.0, 0.5, 0.5, 0.5], "openai:m2", 5)
    assert {h["name"] for h in hits} == {"computeDiscount", "sendEmail", "Invoice"}


def test_sync_without_embedder_keeps_keyword_search(graph, monkeypatch):
    monkeypatch.setattr(indexer, "probe", lambda _s: "fastembed is not installed")
    result = _sync(graph, FakeEmbedder(), monkeypatch)
    assert result["items"] == 3 and result["embedded"] == 0 and "fastembed" in result["error"]
    with graph.session() as s:
        assert graph_index.search_ready(s)["keyword"]
        assert [r["name"] for r in graph_index.keyword_candidates(s, "discount", 5)] == ["computeDiscount"]


def test_build_text_truncates_and_skips_docstring_already_in_source():
    src = 'def f():\n    """Doc."""\n    return 1'
    text = indexer.build_text("Function", "f", "pkg/a.py", "Doc.", src)
    assert text.startswith("Function f\npkg/a.py\ndef f()") and text.count("Doc.") == 1
    assert len(indexer.build_text("Class", "C", "a", None, "x" * 5000)) == indexer.MAX_TEXT_CHARS


def test_to_artifacts_skips_module_and_dependencies_and_reads_source_from_disk(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("a = 1\ndef g():\n    return a\n", encoding="utf-8")
    root = tmp_path.as_posix()
    rows = [
        {"uid": "1", "kind": "Function", "name": "g", "path": f.as_posix(), "line": 2, "end_line": 3, "source": None},
        {"uid": "2", "kind": "Function", "name": "<module>", "path": f.as_posix(), "line": 1, "end_line": 3},
        {"uid": "3", "kind": "Class", "name": "Dep", "path": f.as_posix(), "line": 1, "dep": True},
    ]
    (art,) = indexer.to_artifacts(rows, root)
    assert art.uid == "1" and art.kind == "Function"
    assert art.text == "Function g\nm.py\ndef g():\n    return a"
    assert art.keywords == "py def return"  # one-letter words are dropped


def test_embedder_helpers():
    assert embedders.api_url("http://h:1234", "embeddings") == "http://h:1234/v1/embeddings"
    assert embedders.api_url("http://h/v1/", "models") == "http://h/v1/models"
    assert embedders._prefixes("Xenova/multilingual-e5-small") == ("query: ", "passage: ")
    assert embedders._prefixes("text-embedding-nomic-embed-text-v1.5")[0] == "search_query: "
    assert embedders._normalise([3, 4]) == pytest.approx([0.6, 0.8])
    assert embedders.probe(settings.EmbedSettings("openai", "", "http://x", "")) == "no embedding model configured"


def test_openai_compatible_embedder_batches_and_normalises(monkeypatch):
    calls = []

    class Resp:
        status_code = 200

        def __init__(self, n):
            self._n = n

        def json(self):
            return {"data": [{"index": i, "embedding": [2.0, 0.0]} for i in reversed(range(self._n))]}

    def post(url, json, headers, timeout):
        calls.append((url, json["input"], headers.get("Authorization")))
        return Resp(len(json["input"]))

    import requests
    monkeypatch.setattr(requests, "post", post)
    emb = embedders.OpenAICompatibleEmbedder(settings.EmbedSettings("openai", "nomic-embed", "http://h", "k"))
    emb.batch_size = 2
    out = emb.embed_documents(["a", "b", "c"])
    assert out == [[1.0, 0.0]] * 3
    assert [c[1] for c in calls] == [["search_document: a", "search_document: b"], ["search_document: c"]]
    assert calls[0][0] == "http://h/v1/embeddings" and calls[0][2] == "Bearer k"


def test_settings_save_validates_and_keeps_secrets(monkeypatch, tmp_path):
    from code_visualizer.cli import config_manager

    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / ".env")
    for key in settings.DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    settings.save({"SEARCH_EMBED_PROVIDER": "OpenAI", "CHAT_LLM_API_KEY": "sk-1", "UNKNOWN": "x"})
    assert settings.embed_settings().provider == "openai"
    assert settings.llm_settings().api_key == "sk-1"
    settings.save({"CHAT_LLM_API_KEY": None, "CHAT_LLM_MODEL": "gemma"})
    pub = settings.public_config()
    assert pub["CHAT_LLM_API_KEY"] == "" and pub["CHAT_LLM_API_KEY_SET"] is True
    assert pub["CHAT_LLM_MODEL"] == "gemma"
    with pytest.raises(ValueError):
        settings.save({"SEARCH_EMBED_PROVIDER": "cohere"})
    with pytest.raises(ValueError):
        settings.save({"CHAT_LLM_URL": "http://x\nSEMANTIC_SEARCH=true"})


def test_same_uid_in_two_labels_is_written_to_each_label(graph, monkeypatch):
    """A uid is unique per label only (minified code: a Function and a Class `T`)."""
    with graph.session() as s:
        s.run("MERGE (c:Class {uid: 'a'}) SET c.name = 'Collide', c.path = '/r/a.py', c.line_number = 9")
    emb = FakeEmbedder()
    result = _sync(graph, emb, monkeypatch)
    if result["error"] and "extension" in result["error"]:
        pytest.skip(result["error"])
    assert result["embedded"] == 4
    assert _sync(graph, emb, monkeypatch)["embedded"] == 0
    with graph.session() as s:
        texts = {r["n"]: r["t"] for r in s.run_native(
            "MATCH (n) WHERE n.uid = 'a' RETURN n.name AS n, n.search_text AS t").data()}
    assert texts["computeDiscount"].startswith("compute discount")
    assert texts["Collide"].startswith("collide")
