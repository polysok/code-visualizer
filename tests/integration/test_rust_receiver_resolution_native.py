"""Rust method calls bind to the receiver's type, not to any same-named function.

Regression for a false positive seen on a real repository: `run.control.lock()`
(a std `Mutex`) was linked to an unrelated project method `Store::lock`.
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
repo = root / "repo"
(repo / "src" / "store").mkdir(parents=True)
files = {
    "src/lib.rs": "pub trait Embedder { fn embed_document(&self, t: &str) -> Vec<f32>; }\n",
    "src/e5.rs": "use crate::Embedder;\npub struct E5;\n"
                 "impl Embedder for E5 { fn embed_document(&self, t: &str) -> Vec<f32> { vec![] } }\n",
    "src/store/tests.rs": "use crate::Embedder;\nstruct Other;\n"
                          "impl Embedder for Other { fn embed_document(&self, t: &str) -> Vec<f32> { vec![] } }\n",
    "src/cancel.rs": "pub struct CancelToken;\nimpl CancelToken { pub fn cancel(&self) {} }\n",
    "src/run.rs": "use std::sync::Mutex;\nuse crate::cancel::CancelToken;\n"
                  "pub struct PipelineRun { control: Mutex<Control> }\n"
                  "pub struct Control { pub cancel: CancelToken }\n",
    "src/store.rs": "use crate::Embedder;\nuse std::sync::Mutex;\n"
                    "pub struct Store { embedder: Box<dyn Embedder>, conn: Mutex<u8> }\n"
                    "impl Store {\n"
                    "    pub fn lock(&self) {}\n"
                    "    pub fn save(&self) { self.embedder.embed_document(\"x\"); }\n"
                    "}\n",
    "src/cmd.rs": "use crate::run::PipelineRun;\n"
                  "pub fn stop(run: State<'_, PipelineRun>) {\n"
                  "    let control = run.control.lock().unwrap();\n"
                  "    control.cancel.cancel();\n"
                  "}\n",
}
for rel, text in files.items():
    (repo / rel).write_text(text, encoding="utf-8")

manager = Manager(str(root / "db"))
try:
    jobs = JobManager()
    async def index():
        builder = GraphBuilder(manager, jobs, asyncio.get_running_loop())
        await builder.build_graph_from_path_async(repo, job_id=jobs.create_job(str(repo), is_dependency=False))
    asyncio.run(index())
    with manager.get_driver().session() as s:
        rows = s.run("MATCH (a)-[r:CALLS|HEURISTIC_CALLS]->(b) "
                     "RETURN a.name AS a, b.name AS b, b.path AS bp, type(r) AS t, r.line_number AS l").data()
    edges = {(r["a"], r["b"], Path(r["bp"]).name, r["t"], r["l"]) for r in rows}
    assert not any(b == "lock" for _, b, _, _, _ in edges), edges          # std Mutex::lock
    assert ("stop", "cancel", "cancel.rs", "CALLS", 4) in edges, edges        # field chain across files
    assert ("save", "embed_document", "e5.rs", "HEURISTIC_CALLS", 6) in edges, edges  # dyn Trait
    assert not any(bp == "tests.rs" for _, _, bp, _, _ in edges), edges       # prod impl preferred
    print("rust receiver checks passed")
finally:
    manager.close_driver()
'''


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["ladybugdb"])
def test_rust_receiver_resolution_on_native_backend(backend, tmp_path):
    src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(src) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run([sys.executable, "-c", NATIVE_SCRIPT, backend, str(tmp_path)],
                          capture_output=True, text=True, timeout=300, env=env)
    if proc.returncode == 77:
        pytest.skip(f"{backend} backend not installed")
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "rust receiver checks passed" in proc.stdout
