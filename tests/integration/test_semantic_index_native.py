"""Search data written into the graph at indexing time and by the watcher (real LadybugDB).

A deterministic fake embedder replaces the model: the test checks which texts
get (re-)embedded, not the quality of the vectors.
"""

import os
import subprocess
import sys

import pytest

NATIVE_SCRIPT = r'''
import asyncio
import hashlib
import importlib.util
import os
import sys
import time
from pathlib import Path

backend, root = sys.argv[1], Path(sys.argv[2])
module = "kuzu" if backend == "kuzudb" else "ladybug"
if importlib.util.find_spec(module) is None:
    raise SystemExit(77)
if backend == "ladybugdb":
    from code_visualizer.core import ladybugdb_unavailable_reason
    if ladybugdb_unavailable_reason():
        raise SystemExit(77)
    from code_visualizer.core.database_ladybug import LadybugDBManager as Manager
else:
    from code_visualizer.core.database_ladybug import LadybugDBManager as Manager

os.environ.update({"DEFAULT_DATABASE": backend, "CVIZ_RUNTIME_DB_TYPE": backend,
                   "CVIZ_EMBEDDED_BUFFER_POOL_MB": "256", "SCIP_INDEXER": "false",
                   "ENABLE_VECTOR_RESOLVE": "false", "ENABLE_INHERIT_RESOLVE": "false",
                   "CVIZ_EVENTS_FILE": str(root / "events.jsonl"),
                   "SEMANTIC_SEARCH": "true", "SEARCH_EMBED_PROVIDER": "openai",
                   "SEARCH_EMBED_URL": "http://fake", "SEARCH_EMBED_MODEL": "fake-model"})

from code_visualizer.core.jobs import JobManager
from code_visualizer.core.watcher import CodeWatcher
from code_visualizer.semantic import embedders, graph_index, indexer
from code_visualizer.tools.graph_builder import GraphBuilder

EMBEDDED = []

def fake_embed(self, texts):
    EMBEDDED.extend(texts)
    return [[b / 255.0 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts]

embedders.OpenAICompatibleEmbedder._embed = fake_embed

repo = (root / "repo")
repo.mkdir()
repo = repo.resolve()
(repo / "pricing.py").write_text(
    "def compute_discount(order):\n"
    "    \"\"\"Customer discount applied at checkout.\"\"\"\n"
    "    return order.total * 0.1\n"
    "\n"
    "class Invoice:\n"
    "    def render(self):\n"
    "        return 'pdf'\n", encoding="utf-8")
(repo / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

manager = Manager(str(root / "db"))
P = lambda name: (repo / name).as_posix()

def store_items():
    with manager.get_driver().session() as s:
        rows = graph_index.collect(s, repo.as_posix() + "/")
        stats = graph_index.stats(s, "openai:fake-model", repo.as_posix() + "/")
    return {(r["kind"], r["name"]): r for r in rows if r["search_text"]}, stats

try:
    jobs = JobManager()
    holder = {}

    async def index():
        builder = GraphBuilder(manager, jobs, asyncio.get_running_loop())
        holder["b"] = builder
        await builder.build_graph_from_path_async(repo, job_id=jobs.create_job(str(repo), is_dependency=False))
    asyncio.run(index())
    builder = holder["b"]

    items, stats = store_items()
    assert set(items) == {("Function", "compute_discount"), ("Class", "Invoice"),
                          ("Function", "render"), ("Function", "helper")}, items
    discount = items[("Function", "compute_discount")]
    assert discount["line"] == 1 and discount["path"] == P("pricing.py"), discount
    assert discount["search_text"].startswith("compute discount"), discount["search_text"]
    assert "customer discount applied checkout" in discount["search_text"], discount["search_text"]
    with manager.get_driver().session() as s:
        hits = graph_index.keyword_candidates(s, "checkout", 5)
    assert [h["name"] for h in hits] == ["compute_discount"], hits
    assert stats == {"items": 4, "embedded": 4}, stats
    assert len(EMBEDDED) == 4, EMBEDDED

    # Nothing changed: nothing re-embedded.
    EMBEDDED.clear()
    res = indexer.sync(manager.get_driver().session, repo.as_posix())
    assert res["error"] is None and res["embedded"] == 0 and EMBEDDED == [], res

    # Watcher: the re-indexed files' artifacts are recreated; the moved helper
    # (same text) gets its vector back, only the edited function is embedded.
    # The deleted class leaves the indexes with its node.
    watcher = CodeWatcher(builder, jobs)
    watcher.watch_directory(str(repo), perform_initial_scan=False)
    handler = watcher.handlers[str(repo)]
    (repo / "util.py").write_text("\n\ndef helper():\n    return 1\n", encoding="utf-8")
    handler._handle_modification(P("util.py"))
    (repo / "pricing.py").write_text(
        "def compute_discount(order):\n"
        "    \"\"\"Customer discount applied at checkout.\"\"\"\n"
        "    return order.total * 0.2\n", encoding="utf-8")
    handler._handle_modification(P("pricing.py"))
    deadline = time.time() + 30
    while time.time() < deadline:
        items, stats = store_items()
        # Wait for BOTH files: pricing.py is re-indexed after util.py, and on a
        # slow runner the class can already be gone while compute_discount is
        # not back yet (the check then saw helper alone).
        if ("Class", "Invoice") not in items and items.get(("Function", "helper")) \
                and items[("Function", "helper")]["line"] == 3 \
                and ("Function", "compute_discount") in items \
                and stats["embedded"] == stats["items"] and EMBEDDED:
            break
        time.sleep(0.2)
    assert set(items) == {("Function", "compute_discount"), ("Function", "helper")}, items
    assert items[("Function", "helper")]["line"] == 3, items
    assert stats == {"items": 2, "embedded": 2}, stats
    assert len(EMBEDDED) == 1 and "0.2" in EMBEDDED[0], EMBEDDED

    # Removal drops the file's items.
    (repo / "util.py").unlink()
    handler._handle_removal(P("util.py"))
    items, _ = store_items()
    assert set(items) == {("Function", "compute_discount")}, items
    watcher.stop()
    print("semantic index checks passed")
finally:
    manager.close_driver()
'''


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["ladybugdb"])
def test_semantic_index_on_native_backend(backend, tmp_path):
    src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(src) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, "-c", NATIVE_SCRIPT, backend, str(tmp_path)],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if proc.returncode == 77:
        pytest.skip(f"{backend} backend not installed")
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "semantic index checks passed" in proc.stdout
