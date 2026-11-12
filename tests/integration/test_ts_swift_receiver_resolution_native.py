"""TypeScript and Swift method calls bind to the receiver's declared type.

Regressions:
* TypeScript: `this.mu.clear()` on a `Map` was linked, as a precise edge, to an
  unrelated project method `Store.clear` (the only project symbol named
  `clear`); calls on parameters and locals were only heuristic.
* Swift: calls on typed properties and parameters were dropped because their
  type was recorded as `Unknown`.
"""

import os
import subprocess
import sys

import pytest

NATIVE_SCRIPT = r'''
import asyncio, importlib.util, os, sys
from pathlib import Path

backend, root = sys.argv[1], Path(sys.argv[2])
if importlib.util.find_spec("kuzu" if backend == "kuzudb" else "ladybug") is None:
    raise SystemExit(77)
if backend == "ladybugdb":
    from code_visualizer.core import ladybugdb_unavailable_reason
    if ladybugdb_unavailable_reason():
        raise SystemExit(77)
    from code_visualizer.core.database_ladybug import LadybugDBManager as Manager
else:
    from code_visualizer.core.database_ladybug import LadybugDBManager as Manager
from code_visualizer.core.jobs import JobManager
from code_visualizer.tools.graph_builder import GraphBuilder

os.environ.update({"DEFAULT_DATABASE": backend, "CVIZ_RUNTIME_DB_TYPE": backend,
                   "CVIZ_EMBEDDED_BUFFER_POOL_MB": "256", "SCIP_INDEXER": "false",
                   "ENABLE_VECTOR_RESOLVE": "false", "ENABLE_INHERIT_RESOLVE": "false",
                   "CVIZ_EVENTS_FILE": str(root / "events.jsonl")})

PROJECTS = {
    "ts": {
        "store.ts": "export class Store { clear(): void {} save(): void {} }\n",
        "repo.ts": "export interface Repo { find(): void; }\n"
                   "export class SqlRepo implements Repo { find(): void {} }\n",
        "holder.ts": "import { Store } from './store';\nexport class Holder { inner: Store = new Store(); }\n",
        "service.ts": "import { Store } from './store';\nimport { Repo } from './repo';\nimport { Holder } from './holder';\n"
                      "export class Service {\n"
                      "  private mu: Map<string, number> = new Map();\n"
                      "  constructor(private store: Store, private repo: Repo, private holder: Holder) {}\n"
                      "  run(p: Store, seen: Set<string>): void {\n"
                      "    this.mu.clear();\n"
                      "    this.store.save();\n"
                      "    p.save();\n"
                      "    const local = new Store();\n"
                      "    local.save();\n"
                      "    this.repo.find();\n"
                      "    this.holder.inner.save();\n"
                      "    seen.clear();\n"
                      "  }\n"
                      "}\n",
    },
    "swift": {
        "Store.swift": "class Store { func lock() {} func save() {} }\n",
        "Repo.swift": "protocol Repo { func find() }\nclass SqlRepo: Repo { func find() {} }\n",
        "Holder.swift": "class Holder { let inner: Store = Store() }\n",
        "Service.swift": "import Foundation\n"
                         "class Service {\n"
                         "  let mu = NSLock()\n"
                         "  let store: Store\n"
                         "  let repo: Repo\n"
                         "  let holder: Holder\n"
                         "  init(store: Store, repo: Repo, holder: Holder) { self.store = store; self.repo = repo; self.holder = holder }\n"
                         "  func run(p: Store) {\n"
                         "    mu.lock()\n"
                         "    store.save()\n"
                         "    p.save()\n"
                         "    let local = Store()\n"
                         "    local.save()\n"
                         "    self.repo.find()\n"
                         "    self.holder.inner.save()\n"
                         "  }\n"
                         "}\n",
    },
}
EXPECTED = {
    "ts": {
        "present": {(9, "save", "store.ts", "CALLS"), (10, "save", "store.ts", "CALLS"),
                    (12, "save", "store.ts", "CALLS"), (13, "find", "repo.ts", "HEURISTIC_CALLS"),
                    (14, "save", "store.ts", "CALLS")},
        "absent_names": {"clear"},
    },
    "swift": {
        "present": {(10, "save", "Store.swift", "CALLS"), (11, "save", "Store.swift", "CALLS"),
                    (13, "save", "Store.swift", "CALLS"), (14, "find", "Repo.swift", "HEURISTIC_CALLS"),
                    (15, "save", "Store.swift", "CALLS")},
        "absent_names": {"lock"},
    },
}

for key, files in PROJECTS.items():
    repo = root / key
    repo.mkdir()
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    manager = Manager(str(root / f"db_{key}"))
    try:
        jobs = JobManager()
        async def index():
            builder = GraphBuilder(manager, jobs, asyncio.get_running_loop())
            await builder.build_graph_from_path_async(repo, job_id=jobs.create_job(str(repo), is_dependency=False))
        asyncio.run(index())
        with manager.get_driver().session() as s:
            rows = s.run("MATCH (a)-[r:CALLS|HEURISTIC_CALLS]->(b) WHERE a.name = 'run' "
                         "RETURN b.name AS b, b.path AS bp, type(r) AS t, r.line_number AS l").data()
    finally:
        manager.close_driver()
    edges = {(r["l"], r["b"], Path(r["bp"]).name, r["t"]) for r in rows}
    missing = EXPECTED[key]["present"] - edges
    assert not missing, (key, "missing", missing, edges)
    wrong = {e for e in edges if e[1] in EXPECTED[key]["absent_names"]}
    assert not wrong, (key, "false positives", wrong)
print("ts/swift receiver checks passed")
'''


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["ladybugdb"])
def test_ts_swift_receiver_resolution_on_native_backend(backend, tmp_path):
    src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(src) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run([sys.executable, "-c", NATIVE_SCRIPT, backend, str(tmp_path)],
                          capture_output=True, text=True, timeout=300, env=env)
    if proc.returncode == 77:
        pytest.skip(f"{backend} backend not installed")
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "ts/swift receiver checks passed" in proc.stdout
