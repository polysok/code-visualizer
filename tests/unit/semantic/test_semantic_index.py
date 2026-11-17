"""semantic/: settings, embedder helpers, text building and the graph sync."""
import pytest

from code_visualizer.semantic import embedders, indexer, settings
from code_visualizer.semantic import graph_index


class SpreadEmbedder:
    """Distinct vectors of ``dim`` derived from a hash of the text."""

    def __init__(self, dim=16):
        self.dim = dim

    def embed_documents(self, texts):
        import hashlib
        return [[b / 255 + 0.01 for b in hashlib.sha256(t.encode()).digest()[:self.dim]] for t in texts]


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


def test_new_artifact_is_embedded_without_crashing_the_indexed_table(graph, monkeypatch):
    """LadybugDB crashes (SIGSEGV) when a vector is set on an indexed node that
    had none: the index is dropped for such writes, then rebuilt."""
    def add(prefix, count):  # enough nodes for the HNSW insert to prune neighbours
        with graph.session() as s:
            s.run("UNWIND $rows AS row MERGE (f:Function {uid: row.uid}) SET f.name = row.name, "
                  "f.path = '/r/many.py', f.line_number = row.line, f.source = 'def ' + row.name + '(): pass'",
                  rows=[{"uid": f"{prefix}{i}", "name": f"{prefix}fn{i}", "line": i} for i in range(count)])

    emb = SpreadEmbedder()
    add("old", 400)
    result = _sync(graph, emb, monkeypatch)
    if result["error"] and "extension" in result["error"]:
        pytest.skip(result["error"])
    add("new", 600)
    with graph.session() as s:
        s.run("MERGE (f:Function {uid: 'n'}) SET f.name = 'refundOrder', f.path = '/r/n.py', "
              "f.line_number = 1, f.source = 'def refundOrder(): pass'")
    assert _sync(graph, emb, monkeypatch)["embedded"] == 601
    with graph.session() as s:
        assert graph_index.search_ready(s) == {"keyword": True, "vector": True}
        assert graph_index.stats(s, "openai:m1") == {"items": 1004, "embedded": 1004}
        assert graph_index.vector_candidates(s, [0.5] * 16, "openai:m1", 5)
        # Stopped between the drop and the rebuild: the next sync rebuilds it.
        graph_index.drop_vector_indexes(s)
        assert graph_index.indexed_dim(s) == 16
    _sync(graph, emb, monkeypatch)
    with graph.session() as s:
        assert graph_index.active_dim(s) == 16


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


@pytest.fixture
def ai_env():
    """settings.save() writes the AI keys into os.environ: restore them after the test."""
    import os

    saved = {key: os.environ.get(key) for key in settings.DEFAULTS}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_ai_settings_are_not_frozen_into_the_environment(ai_env, monkeypatch, tmp_path):
    """The CLI copies its configuration into the environment, which wins over the
    file: the AI search settings stay out, so a save from the page applies to
    every running server."""
    import os
    from code_visualizer.cli import main as cli_main

    home = tmp_path / "home"
    (home / ".code-visualizer").mkdir(parents=True)
    (home / ".code-visualizer" / ".env").write_text(
        "SEARCH_EMBED_MODEL=nomic\nCVIZ_TEST_OTHER_KEY=1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SEARCH_EMBED_MODEL", raising=False)
    monkeypatch.setenv("CVIZ_TEST_OTHER_KEY", "")
    monkeypatch.delenv("CVIZ_TEST_OTHER_KEY")
    cli_main._load_credentials()
    assert "SEARCH_EMBED_MODEL" not in os.environ
    assert os.environ.get("CVIZ_TEST_OTHER_KEY") == "1"


def test_settings_save_validates_and_keeps_secrets(ai_env, monkeypatch, tmp_path):
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


def test_syncs_never_run_at_the_same_time(monkeypatch):
    """A sync rebuilding the vector index while another writes vectors
    without it crashes the database: syncs are serialized."""
    import threading
    import time

    active, overlaps = [0], []

    def slow(*_a, **_k):
        active[0] += 1
        overlaps.append(active[0])
        time.sleep(0.05)
        active[0] -= 1
        return {}

    monkeypatch.setattr(indexer, "_sync", slow)
    threads = [threading.Thread(target=indexer.sync, args=(None, "/r")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == [1, 1, 1, 1]


def test_a_stopped_vectorization_keeps_its_vectors_and_the_index(graph, monkeypatch):
    """Changing the embedding model stops the running vectorization between two
    batches: what was written stays, the vector index is rebuilt."""
    import threading

    def add(prefix, count):
        with graph.session() as s:
            s.run("UNWIND $rows AS row MERGE (f:Function {uid: row.uid}) SET f.name = row.name, "
                  "f.path = '/r/many.py', f.line_number = row.line, f.source = 'def ' + row.name + '(): pass'",
                  rows=[{"uid": f"{prefix}{i}", "name": f"{prefix}fn{i}", "line": i} for i in range(count)])

    result = _sync(graph, SpreadEmbedder(), monkeypatch)
    if result["error"] and "extension" in result["error"]:
        pytest.skip(result["error"])
    add("new", 200)
    stop = threading.Event()

    class StopAfterFirstBatch(SpreadEmbedder):
        def embed_documents(self, texts):
            stop.set()
            return super().embed_documents(texts)

    result = _sync(graph, StopAfterFirstBatch(), monkeypatch, stop=stop)
    assert result["error"] == indexer.STOPPED
    with graph.session() as s:
        assert graph_index.search_ready(s)["vector"]
        assert graph_index.stats(s, "openai:m1")["embedded"] == 3 + indexer.EMBED_CHUNK


def test_an_older_versions_indexes_no_longer_block_the_writes(tmp_path, monkeypatch):
    """A database of an older version has full-text indexes and vector indexes
    under other names: the writes failed ("Trying to delete from an index on
    table Function but its extension is not loaded"), and the watcher with
    them. Opened, the database loads their extensions and drops them."""
    from code_visualizer.core import ladybugdb_unavailable_reason
    from code_visualizer.core.database_ladybug import LadybugDBManager

    lb = pytest.importorskip("ladybug")
    reason = ladybugdb_unavailable_reason()
    if reason:
        pytest.skip(reason)
    path = str(tmp_path / "db")
    manager = LadybugDBManager(path)
    with manager.get_driver().session() as s:
        s.run("MERGE (f:Function {uid: 'a'}) SET f.name = 'old', f.path = '/r/a.py'")
    manager.close_driver()

    db = lb.Database(path)
    conn = lb.Connection(db)
    try:
        for ext in ("fts", "vector"):
            conn.execute(f"LOAD {ext}")
    except Exception as exc:  # noqa: BLE001 - not installed here (no network)
        pytest.skip(f"LadybugDB extensions unavailable: {exc}")
    conn.execute("ALTER TABLE Function ADD search_embedding_3 FLOAT[3]")
    conn.execute("CALL CREATE_FTS_INDEX('Function', 'cgc_fts_Function', ['name'])")
    conn.execute("CALL CREATE_VECTOR_INDEX('Function', 'cgc_vec_Function_3', 'search_embedding_3', metric := 'cosine')")
    conn.close()
    db.close()

    # A new process: no extension loaded until the manager opens the database.
    manager = LadybugDBManager(path)
    try:
        with manager.get_driver().session() as s:
            s.run("MATCH (f:Function {uid: 'a'}) DETACH DELETE f")
            left = [r["n"] for r in s.run_native("CALL SHOW_INDEXES() RETURN index_name AS n").data()]
        assert not [n for n in left if n.startswith("cgc_")]
    finally:
        manager.close_driver()
