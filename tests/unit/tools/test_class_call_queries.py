"""Native regression for Function → Class constructor call lookups (#1723)."""

import os
import subprocess
import sys

import pytest

from code_visualizer.tools.code_finder import CodeFinder


NATIVE_SCRIPT = r'''
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

backend = sys.argv[1]
module = "kuzu" if backend == "kuzudb" else "ladybug"
if importlib.util.find_spec(module) is None:
    raise SystemExit(77)
if backend == "ladybugdb":
    from code_visualizer.core import ladybugdb_unavailable_reason
    reason = ladybugdb_unavailable_reason()
    if reason:
        print(reason)
        raise SystemExit(77)
    from code_visualizer.core.database_ladybug import LadybugDBManager as Manager
else:
    from code_visualizer.core.database_ladybug import LadybugDBManager as Manager
from code_visualizer.tools.code_finder import CodeFinder

class Adapter:
    def get_driver(self, graph_name=None):
        return driver
    def get_backend_type(self):
        return backend

os.environ["CVIZ_EMBEDDED_BUFFER_POOL_MB"] = "256"
with tempfile.TemporaryDirectory() as root:
    repo = Path(root) / "repo"
    other = Path(root) / "repo-other"
    repo.mkdir()
    other.mkdir()
    path = (repo / "app.py").as_posix()
    class_path = (repo / "models.py").as_posix()
    other_path = (other / "models.py").as_posix()
    manager = Manager(str(Path(root) / "db"))
    driver = manager.get_driver()
    try:
        with driver.session() as session:
            for name in ("entry", "build", "leaf"):
                session.run(
                    "CREATE (:Function {uid: $uid, name: $name, path: $path, "
                    "line_number: 3, is_dependency: false})",
                    uid=name, name=name, path=path,
                )
            session.run(
                "CREATE (:Function {uid: 'outsider', name: 'outsider', path: $path, "
                "line_number: 3, is_dependency: false})", path=other_path,
            )
            for uid, node_path in (("widget", class_path), ("other-widget", other_path)):
                session.run(
                    "CREATE (:Class {uid: $uid, name: 'Widget', path: $path, "
                    "line_number: 10, is_dependency: false})", uid=uid, path=node_path,
                )
            session.run("MATCH (a:Function {uid: 'entry'}), (b:Function {uid: 'build'}) "
                        "CREATE (a)-[:CALLS {line_number: 5}]->(b)")
            session.run("MATCH (a:Function {uid: 'build'}), (b:Class {uid: 'widget'}) "
                        "CREATE (a)-[:CALLS {line_number: 8}]->(b)")
            session.run("MATCH (a:Function {uid: 'build'}), (b:Function {uid: 'leaf'}) "
                        "CREATE (a)-[:HEURISTIC_CALLS {line_number: 9}]->(b)")
            session.run("MATCH (a:Function {uid: 'outsider'}), (b:Class {uid: 'other-widget'}) "
                        "CREATE (a)-[:CALLS {line_number: 8}]->(b)")
        finder = CodeFinder(Adapter())
        def edges(rows):
            return {(r["caller_name"], r["callee_name"]) for r in rows}

        for with_path in (False, True):
            target_path = class_path if with_path else None
            callers = finder.who_calls_function("Widget", path=target_path, repo_path=str(repo))
            assert [r["caller_function"] for r in callers] == ["build"], callers
            assert callers[0]["target_file_path"] == class_path
            callees = finder.what_does_function_call("build", path=path if with_path else None,
                                                     repo_path=str(repo))
            assert {r["called_function"] for r in callees} == {"Widget", "leaf"}, callees
            assert edges(finder.find_all_callers("Widget", path=target_path, repo_path=str(repo))) == {
                ("entry", "build"), ("build", "Widget"),
            }
            assert edges(finder.find_all_callees("entry", path=path if with_path else None,
                                                repo_path=str(repo))) == {
                ("entry", "build"), ("build", "Widget"), ("build", "leaf"),
            }
        # Cover the existing missing-path fallback and the traversal-depth bound.
        fallback = finder.who_calls_function("Widget", path=str(repo / "missing.py"), repo_path=str(repo))
        assert [r["caller_function"] for r in fallback] == ["build"], fallback
        assert edges(finder.find_all_callers("Widget", repo_path=str(repo), depth=1)) == {("build", "Widget")}
        assert finder.find_all_callers("Widget", path=other_path, repo_path=str(repo)) == []
        assert [r["caller_function"] for r in finder.who_calls_function("leaf", repo_path=str(repo))] == ["build"]
        print("constructor and function queries preserve scope and depth")
    finally:
        manager.close_driver()
'''


@pytest.mark.parametrize("backend", ["ladybugdb"])
def test_class_constructor_queries_on_native_backend(backend):
    # Native Kuzu and Ladybug extensions must run in separate interpreters.
    result = subprocess.run(
        [sys.executable, "-c", NATIVE_SCRIPT, backend],
        capture_output=True, text=True, timeout=90, env=os.environ.copy(),
    )
    if result.returncode == 77:
        pytest.skip(result.stdout.strip() or f"{backend} is unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preserve scope and depth" in result.stdout


def test_path_conversion_retains_the_existing_500_edge_limit():
    row = {"path_nodes": [{"name": "entry", "path": "/repo/app.py"},
                          {"name": "build", "path": "/repo/app.py"}]}
    rows = [{**row, "rels": [{"line_number": line}]} for line in range(501)]
    assert len(CodeFinder._call_path_edges(rows)) == 500
