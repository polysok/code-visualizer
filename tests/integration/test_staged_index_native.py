"""Staged first index (cviz graph, GRAPH_FIRST_PASS_FILES) on a real LadybugDB.

The first stage indexes the files highest in priority (code before tests,
shallow before deep); the second, in the background, indexes the rest and
re-links the whole repository. The end result must be the graph of a full
index: calls and inheritance across the two stages, in both directions.
"""

import os
import subprocess
import sys

import pytest

NATIVE_SCRIPT = r'''
import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
if importlib.util.find_spec("ladybug") is None:
    raise SystemExit(77)
from code_visualizer.core import ladybugdb_unavailable_reason
if ladybugdb_unavailable_reason():
    raise SystemExit(77)

events_file = root / "events.jsonl"
os.environ.update({"CVIZ_EMBEDDED_BUFFER_POOL_MB": "256", "SCIP_INDEXER": "false",
                   "ENABLE_VECTOR_RESOLVE": "false", "ENABLE_INHERIT_RESOLVE": "false",
                   "SEMANTIC_SEARCH": "false", "CVIZ_EVENTS_FILE": str(events_file)})

from code_visualizer.core.database_ladybug import LadybugDBManager
from code_visualizer.core.jobs import JobManager
from code_visualizer.core.staged_index import start_deferred_index
from code_visualizer.tools.graph_builder import GraphBuilder

repo = root / "repo"
(repo / "pkg" / "deep" / "deeper").mkdir(parents=True)
(repo / "tests").mkdir()
repo = repo.resolve()

def write(rel, text):
    (repo / rel).write_text(text, encoding="utf-8")

# First stage (2 files): main.py and util.py, the shallowest code.
write("main.py", "from pkg.deep.deeper.core import Engine, crunch\n\n"
                 "def main():\n    return crunch(Engine())\n")
write("util.py", "def helper():\n    return 1\n")
# Deferred: deeper code calling into the first stage and back, and a test.
write("pkg/deep/deeper/core.py", "from util import helper\nfrom pkg.deep.base import Base\n\n"
                                 "class Engine(Base):\n    def run(self):\n        return helper()\n\n"
                                 "def crunch(e):\n    return e.run()\n")
write("pkg/deep/base.py", "class Base:\n    def run(self):\n        return 0\n")
write("tests/test_main.py", "from main import main\n\ndef test_main():\n    assert main() == 1\n")

def snapshot(manager):
    with manager.get_driver().session() as s:
        nodes = s.run_native(
            "MATCH (n) WHERE n.path STARTS WITH $p RETURN label(n) AS l, n.name AS name, "
            "n.path AS path, n.line_number AS line", p=repo.as_posix()).data()
        edges = s.run_native(
            "MATCH (a)-[r]->(b) WHERE a.path STARTS WITH $p OR b.path STARTS WITH $p "
            "RETURN label(a) AS la, a.name AS na, a.path AS pa, label(r) AS r, "
            "label(b) AS lb, b.name AS nb, b.path AS pb", p=repo.as_posix()).data()
    canon = lambda rows: sorted(tuple(sorted((k, str(v)) for k, v in r.items())) for r in rows)
    return canon(nodes), canon(edges)

def files_in_graph(manager):
    with manager.get_driver().session() as s:
        rows = s.run_native("MATCH (f:File) WHERE f.path STARTS WITH $p RETURN f.path AS p",
                            p=repo.as_posix()).data()
    return sorted(Path(r["p"]).relative_to(repo).as_posix() for r in rows)

def index(manager, **kwargs):
    jobs = JobManager()
    async def run():
        b = GraphBuilder(manager, jobs, asyncio.get_running_loop())
        b.semantic_search = False
        await b.build_graph_from_path_async(repo, job_id=jobs.create_job(str(repo), is_dependency=False), **kwargs)
        return b
    return asyncio.run(run())

m_full = LadybugDBManager(str(root / "db_full"))
try:
    index(m_full)
    want = snapshot(m_full)
finally:
    m_full.close_driver()

m = LadybugDBManager(str(root / "db_staged"))
try:
    deferred = {}
    builder = index(m, first_pass_files=2, deferred_out=deferred)
    assert files_in_graph(m) == ["main.py", "util.py"], files_in_graph(m)
    assert sorted(Path(f).relative_to(repo).as_posix() for f in deferred["files"]) == [
        "pkg/deep/base.py", "pkg/deep/deeper/core.py", "tests/test_main.py"], deferred["files"]
    assert builder.last_index_summary.get("deferred_files") == 3, builder.last_index_summary

    done = []
    thread = start_deferred_index(deferred, watcher=None, repo=str(repo), on_done=lambda: done.append(1))
    thread.join(120)
    assert done == [1] and deferred == {}, (done, deferred)
    got = snapshot(m)
finally:
    m.close_driver()

assert got[0] == want[0], ("nodes differ", set(got[0]) ^ set(want[0]))
assert got[1] == want[1], ("edges differ", set(got[1]) ^ set(want[1]))
edges = [dict(e) for e in got[1]]
assert any(e["r"] == "CALLS" and e["na"] == "main" and e["nb"] == "crunch" for e in edges), "first stage -> deferred"
assert any(e["r"] == "CALLS" and e["na"] == "run" and e["nb"] == "helper" for e in edges), "deferred -> first stage"
assert any(e["r"] == "INHERITS" and e["na"] == "Engine" and e["nb"] == "Base" for e in edges), "deferred inheritance"

events = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
progress = [e for e in events if e["type"] == "index:progress"]
assert progress and progress[0]["done"] == 0 and progress[-1]["done"] == progress[-1]["total"] == 3, progress
assert any(e["type"] == "index:done" and e.get("deferred") == 3 for e in events), events
print("staged index checks passed")
'''


@pytest.mark.integration
def test_staged_index_matches_a_full_index(tmp_path):
    src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(src) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, "-c", NATIVE_SCRIPT, str(tmp_path)],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if proc.returncode == 77:
        pytest.skip("LadybugDB not installed")
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "staged index checks passed" in proc.stdout
