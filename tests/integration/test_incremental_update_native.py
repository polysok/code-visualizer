"""Incremental update from content fingerprints (cviz update, git hooks, /api/update).

Equivalence on a real LadybugDB: index(A) + incremental update to B gives the
same nodes and edges as a fresh full index of B, and only the files whose
content changed are re-processed.
"""

import os
import subprocess
import sys

import pytest

NATIVE_SCRIPT = r'''
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
if importlib.util.find_spec("ladybug") is None:
    raise SystemExit(77)
from code_visualizer.core import ladybugdb_unavailable_reason
if ladybugdb_unavailable_reason():
    raise SystemExit(77)

os.environ.update({"CVIZ_EMBEDDED_BUFFER_POOL_MB": "256", "SCIP_INDEXER": "false",
                   "ENABLE_VECTOR_RESOLVE": "false", "ENABLE_INHERIT_RESOLVE": "false",
                   "SEMANTIC_SEARCH": "false", "CVIZ_EVENTS_FILE": str(root / "events.jsonl")})

from code_visualizer.core.database_ladybug import LadybugDBManager
from code_visualizer.core.jobs import JobManager
from code_visualizer.core.watcher import RepositoryEventHandler, incremental_update
from code_visualizer.tools.graph_builder import GraphBuilder
from code_visualizer.utils.content_hash import content_hash, file_content_hash

# git's own blob id for "hello\n" (git hash-object)
assert content_hash(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"

repo = (root / "repo")
repo.mkdir()
repo = repo.resolve()

def write(name, text):
    (repo / name).write_text(text, encoding="utf-8")

# --- state A
write("b.py", "def f():\n    return 1\n")
write("a.py", "from b import f\n\ndef main():\n    return f()\n")
write("base.py", "class Base:\n    def run(self):\n        return 0\n")
write("d.py", "from base import Base\n\nclass D(Base):\n    pass\n")
write("same.py", "def untouched():\n    return 3\n")

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

def full_index(manager):
    jobs = JobManager()
    holder = {}
    async def run():
        b = GraphBuilder(manager, jobs, asyncio.get_running_loop())
        holder["b"] = b
        await b.build_graph_from_path_async(repo, job_id=jobs.create_job(str(repo), is_dependency=False))
    asyncio.run(run())
    return holder["b"]

m1 = LadybugDBManager(str(root / "db_incremental"))
try:
    builder = full_index(m1)
    hashes = builder.get_repo_file_hashes(repo)
    assert hashes[(repo / "a.py").as_posix()] == file_content_hash(repo / "a.py"), hashes
    assert incremental_update(builder, repo)["mode"] == "none"

    # --- state B: edit, rename a function, delete, add, keep one file identical
    write("b.py", "def g():\n    return 2\n\ndef f():\n    return g()\n")
    write("a.py", "from b import g\n\ndef main():\n    return g()\n")
    (repo / "d.py").unlink()
    write("e.py", "from base import Base\n\nclass E(Base):\n    def run(self):\n        return main()\n\nfrom a import main\n")
    # touched but identical content (what a branch round-trip does): not re-processed
    os.utime(repo / "same.py", None)

    handler = RepositoryEventHandler(builder, repo, perform_initial_scan=False)
    changed, removed, total = handler.changes_on_disk()
    handler.cancel_timers()
    names = lambda paths: sorted(Path(p).name for p in paths)
    assert names(changed) == ["a.py", "b.py", "e.py"], changed
    assert names(removed) == ["d.py"], removed

    handler.FULL_SYNC_RATIO = 1.0  # force the incremental path on this tiny repo
    result = handler.catch_up_with_disk()
    assert result == {"changed": 3, "removed": 1, "mode": "incremental"}, result
    assert handler.changes_on_disk()[:2] == ([], []), handler.changes_on_disk()
    got = snapshot(m1)
finally:
    m1.close_driver()

m2 = LadybugDBManager(str(root / "db_full"))
try:
    full_index(m2)
    want = snapshot(m2)
finally:
    m2.close_driver()

assert got[0] == want[0], ("nodes differ", set(got[0]) ^ set(want[0]))
assert got[1] == want[1], ("edges differ", set(got[1]) ^ set(want[1]))
print("incremental update checks passed")
'''


@pytest.mark.integration
def test_incremental_update_matches_a_full_index(tmp_path):
    src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(src) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, "-c", NATIVE_SCRIPT, str(tmp_path)],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if proc.returncode == 77:
        pytest.skip("LadybugDB not installed")
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "incremental update checks passed" in proc.stdout
