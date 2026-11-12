"""End-to-end checks for the dependency-graph page backend on a real embedded DB.

Indexes a small repository, then exercises /api/locate and /api/neighborhood
logic (viz/neighborhood.py) and the incremental watcher paths the page relies
on: homonyms in two files, a class method, a line outside any artifact, an
unindexed file, a function added after its callers, and a rename.
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
from code_visualizer.core.jobs import JobManager
from code_visualizer.core.watcher import CodeWatcher
from code_visualizer.tools.graph_builder import GraphBuilder
from code_visualizer.viz import neighborhood as nb

os.environ.update({"DEFAULT_DATABASE": backend, "CVIZ_RUNTIME_DB_TYPE": backend,
                   "CVIZ_EMBEDDED_BUFFER_POOL_MB": "256", "SCIP_INDEXER": "false",
                   "ENABLE_VECTOR_RESOLVE": "false", "ENABLE_INHERIT_RESOLVE": "false",
                   "CVIZ_EVENTS_FILE": str(root / "events.jsonl")})
repo = (root / "repo")
repo.mkdir()
repo = repo.resolve()
(repo / "alpha.py").write_text(
    "def run():\n"
    "    return 1\n", encoding="utf-8")
(repo / "beta.py").write_text(
    "def run():\n"
    "    return 2\n", encoding="utf-8")
(repo / "jobs.py").write_text(
    "from alpha import run\n"
    "\n"
    "class Base:\n"
    "    pass\n"
    "\n"
    "class Job(Base):\n"
    "    def execute(self):\n"
    "        self.prepare()\n"
    "        return run()\n"
    "\n"
    "    def prepare(self):\n"
    "        return None\n", encoding="utf-8")

manager = Manager(str(root / "db"))
P = lambda name: (repo / name).as_posix()
try:
    jobs = JobManager()
    builder_holder = {}

    async def index():
        builder = GraphBuilder(manager, jobs, asyncio.get_running_loop())
        builder_holder["b"] = builder
        await builder.build_graph_from_path_async(repo, job_id=jobs.create_job(str(repo), is_dependency=False))
    asyncio.run(index())
    builder = builder_holder["b"]

    with manager.get_driver().session() as s:
        # Narrowest artifact: method inside class; class between methods; file outside.
        assert nb.locate(s, P("jobs.py"), 8)["name"] == "execute"
        assert nb.locate(s, P("jobs.py"), 10)["name"] == "Job"
        assert nb.locate(s, P("jobs.py"), 999)["kind"] == "File"
        try:
            nb.locate(s, (root / "elsewhere.py").as_posix(), 1)
            raise AssertionError("unindexed file must raise")
        except nb.ArtifactNotFound:
            pass

        data = nb.neighborhood(s, path=P("jobs.py"), line=8, kinds=["CALLS"])
        callees = {(n["name"], Path(n["path"]).name, n["line"]) for n in data["nodes"] if n["role"] == "callee"}
        assert ("run", "alpha.py", 1) in callees, callees
        assert ("prepare", "jobs.py", 11) in callees, callees
        assert ("run", "beta.py", 1) not in callees, "homonym in another file must not be merged"
        call_lines = {l["line"] for l in data["links"]}
        assert {8, 9} <= call_lines, call_lines

        alpha = nb.neighborhood(s, path=P("alpha.py"), name="run", line=1)
        beta = nb.neighborhood(s, path=P("beta.py"), name="run", line=1)
        assert any(n["name"] == "execute" for n in alpha["nodes"] if n["role"] == "caller")
        assert not any(n["role"] == "caller" for n in beta["nodes"]), beta["nodes"]

        inh = nb.neighborhood(s, path=P("jobs.py"), line=6, kinds=["INHERITS"])
        assert {(n["name"], n["role"]) for n in inh["nodes"]} == {("Job", "center"), ("Base", "parent")}

    # Watcher: a function added after its caller, then a rename.
    watcher = CodeWatcher(builder, jobs)
    watcher.watch_directory(str(repo), perform_initial_scan=False)
    handler = watcher.handlers[str(repo)]
    (repo / "late_a.py").write_text("def late_caller():\n    return brand_new_helper(1)\n", encoding="utf-8")
    handler._handle_modification(P("late_a.py"))
    (repo / "late_b.py").write_text("def brand_new_helper(x):\n    return x\n", encoding="utf-8")
    handler._handle_modification(P("late_b.py"))
    with manager.get_driver().session() as s:
        late = nb.neighborhood(s, path=P("late_b.py"), line=1)
    assert any(n["name"] == "late_caller" for n in late["nodes"] if n["role"] == "caller"), late["nodes"]

    (repo / "late_b.py").rename(repo / "late_c.py")
    handler._handle_removal(P("late_b.py"))
    handler._handle_modification(P("late_c.py"))
    with manager.get_driver().session() as s:
        try:
            nb.locate(s, P("late_b.py"), 1)
            raise AssertionError("renamed file must leave no ghost node")
        except nb.ArtifactNotFound:
            pass
        moved = nb.neighborhood(s, path=P("late_c.py"), line=1)
        homonyms = nb.resolve_symbol(s, "brand_new_helper")
    assert any(n["name"] == "late_caller" for n in moved["nodes"] if n["role"] == "caller"), moved["nodes"]
    assert [Path(m["path"]).name for m in homonyms] == ["late_c.py"], homonyms

    events = [json.loads(l) for l in (root / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "graph:changed" and P("late_c.py") in e["files"] for e in events), events
    watcher.stop()
    print("dependency graph checks passed")
finally:
    manager.close_driver()
'''


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["ladybugdb"])
def test_dependency_graph_on_native_backend(backend, tmp_path):
    src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(src) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, "-c", NATIVE_SCRIPT, backend, str(tmp_path)],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if proc.returncode == 77:
        pytest.skip(f"{backend} backend not installed")
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "dependency graph checks passed" in proc.stdout
