"""Parameter / HAS_PARAMETER, Module / IMPORTS and ExternalClass / INHERITS are written as a node-only UNWIND followed
by a relationship-only UNWIND, so the embedded wrapper's forced per-row fallback (Kuzu #1605) never fires: it ran one
translated, prepared query per row, the bulk of a full index. The graph must be what the combined per-row query wrote:
nodes only where the old MATCH succeeded, first non-null Module metadata, the last row's values for a repeated IMPORTS
key, and the label-less external fan-out over every populated child label.
"""
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import code_visualizer.core.database_embedded_kuzu as embedded
from code_visualizer.core.database_ladybug import LadybugDBManager
from code_visualizer.tools.indexing.persistence.writer import GraphWriter

REPO = Path("C:/fx3").resolve().as_posix()
A = f"{REPO}/src/a.rs"
GUARD = re.compile(r"UNWIND\s+\$\w+\s+AS\s+\w+")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    # The two native engines must run in separate interpreters: importing
    # both extension libraries can collide on their C++ symbols.
    backend = os.environ.get("CVIZ_SPLIT_TEST_BACKEND", "ladybugdb")
    pytest.importorskip("ladybug" if backend == "ladybugdb" else "kuzu")
    if backend == "ladybugdb":
        from code_visualizer.core import ladybugdb_unavailable_reason
        reason = ladybugdb_unavailable_reason()
        if reason:
            pytest.skip(reason)
    forced = []
    original = embedded.EmbeddedSessionWrapper.run

    def run(self, q, **p):
        if GUARD.search(q) and ("-[" in q or "]->" in q) and re.search(r"MERGE\s*\(\s*\w+\s*:", q):
            forced.append(q)
        return original(self, q, **p)

    monkeypatch.setattr(embedded.EmbeddedSessionWrapper, "run", run)
    manager_class = LadybugDBManager if backend == "ladybugdb" else LadybugDBManager
    manager = manager_class(str(tmp_path / "db"))
    driver = manager.get_driver()
    writer = GraphWriter(driver, SimpleNamespace(get_backend_type=lambda: backend))
    writer.add_repository_to_graph(Path(REPO))
    yield writer, driver, forced, backend
    manager.close_driver()


def _q(driver, cypher):
    with driver.session() as s:
        return s.run(cypher).data()


def _imp(name, full, line, imported, alias=None, lang="rust"):
    return {"name": name, "full_import_name": full, "line_number": line, "imported_name": imported, "alias": alias, "lang": lang}


def _fn(name, line, args):
    return {"name": name, "line_number": line, "end_line": line + 5, "args": args, "lang": "rust"}


def test_parameters_follow_occurrence_identity(env):
    writer, driver, forced, backend = env
    writer.add_file_to_graph({"path": A, "repo_path": REPO, "lang": "rust",
                              "functions": [_fn("f", 10, ["a", "b"]), _fn("f", 10, ["a", "c"]), _fn("g", 20, ["a", "a"])]}, "fx", {})
    edges = sorted((r["o"], r["p"], r["l"]) for r in _q(driver, "MATCH (f:Function)-[:HAS_PARAMETER]->(p:Parameter) "
                                                         "RETURN f.occurrence_index AS o, p.name AS p, p.function_line_number AS l"))
    assert edges == [(0, "a", 10), (0, "a", 20), (0, "b", 10), (1, "a", 10), (1, "c", 10)]
    assert len(_q(driver, "MATCH (p:Parameter) RETURN p.uid")) == 4        # (a,10) shared by both occurrences
    assert not forced, forced


def test_imports_metadata_and_repeated_keys(env):
    writer, driver, forced, backend = env
    writer.add_file_to_graph({"path": A, "repo_path": REPO, "lang": "rust", "imports": [
        _imp("M1", "use M1::Baz;", 4, "Baz"), _imp("M1", "use super::*;", 1, "*"), _imp("M1", "pub use M1::Bar;", 2, "Bar", "B"),
        _imp("M1", "pub use M1::*;", 5, "*"), _imp("M1", "use M1::Bar;", 2, "Bar", "B2"),
        _imp("M4", "import M4", 1, "M4", lang=None), _imp("M4", "import M4", 2, "M4b")]}, "fx", {})
    mods = {r["n"]: (r["l"], r["f"]) for r in _q(driver, "MATCH (m:Module) RETURN m.name AS n, m.lang AS l, m.full_import_name AS f")}
    assert mods == {"M1": ("rust", "use super::*;"), "M4": ("rust", "import M4")}
    bar = _q(driver, "MATCH (:File)-[r:IMPORTS {line_number: 2, imported_name: 'Bar'}]->(:Module) RETURN r.alias AS a, r.full_import_name AS f")
    assert bar == [{"a": "B2", "f": "use M1::Bar;"}]                      # last sorted row, as row-by-row SETs leave it
    assert len(_q(driver, "MATCH (:File)-[r:IMPORTS]->(:Module) RETURN r.line_number")) == 6
    assert not forced, forced


def test_external_parents_only_for_existing_children(env):
    writer, driver, forced, backend = env
    writer.add_file_to_graph({"path": A, "repo_path": REPO, "lang": "rust",
                              "classes": [{"name": "K", "line_number": 1, "end_line": 2, "lang": "rust"},
                                          {"name": "Status", "line_number": 3, "end_line": 4, "lang": "rust"}],
                              "variables": [{"name": "Status", "line_number": 5, "lang": "rust"}]}, "fx", {})
    x = "__external__"
    writer.write_inheritance_links([
        {"child_name": "K", "path": A, "parent_name": "Base", "resolved_parent_file_path": x, "child_label": "Class"},
        {"child_name": "Status", "path": A, "parent_name": "Legacy", "resolved_parent_file_path": x},
        {"child_name": "Ghost", "path": A, "parent_name": "Nowhere", "resolved_parent_file_path": x, "child_label": "Class"},
    ], [], {})
    assert sorted(r["n"] for r in _q(driver, "MATCH (e:ExternalClass) RETURN e.name AS n")) == ["Base", "Legacy"]
    rels = sorted((r["lab"], r["c"], r["p"], r["k"]) for r in _q(driver, "MATCH (c)-[r:INHERITS]->(p:ExternalClass) "
                                                                  "RETURN label(c) AS lab, c.name AS c, p.name AS p, r.confidence_label AS k"))
    assert rels == [("Class", "K", "Base", "INFERRED"), ("Class", "Status", "Legacy", "INFERRED"),
                    ("Variable", "Status", "Legacy", "INFERRED")]
    assert not forced, forced


def test_js_imports_last_row_per_edge_key(env):
    writer, driver, forced, backend = env
    js = f"{REPO}/src/app.ts"
    writer.add_file_to_graph({"path": js, "repo_path": REPO, "lang": "typescript", "imports": [
        {"source": "./util", "name": "a", "alias": "x1", "line_number": 1},
        {"source": "./util", "name": "b", "alias": "x2", "line_number": 1},   # same edge key: the last row wins
        {"source": "./util", "name": "c", "line_number": 2},
        {"source": "react", "name": "*", "line_number": 3}]}, "fx", {})
    assert sorted(r["n"] for r in _q(driver, "MATCH (m:Module) RETURN m.name AS n")) == ["./util", "react"]
    edges = sorted((r["m"], r["l"], r["i"], r["a"]) for r in _q(driver, "MATCH (:File)-[r:IMPORTS]->(m:Module) "
                                                               "RETURN m.name AS m, r.line_number AS l, r.imported_name AS i, r.alias AS a"))
    assert edges == [("./util", 1, "b", "x2"), ("./util", 2, "c", ""), ("react", 3, "*", "")]
    assert not forced, forced


def test_module_inclusions_only_for_existing_classes(env):
    writer, driver, forced, backend = env
    rb = f"{REPO}/lib/k.rb"
    writer.add_file_to_graph({"path": rb, "repo_path": REPO, "lang": "ruby",
                              "classes": [{"name": "K", "line_number": 1, "end_line": 9, "lang": "ruby"}],
                              "module_inclusions": [{"class": "K", "module": "Comparable"},
                                                    {"class": "K", "module": "Comparable"},
                                                    {"class": "K", "module": "Enumerable"},
                                                    {"class": "Ghost", "module": "Nowhere"}]}, "fx", {})
    assert sorted(r["n"] for r in _q(driver, "MATCH (m:Module) RETURN m.name AS n")) == ["Comparable", "Enumerable"]
    rels = sorted((r["c"], r["m"]) for r in _q(driver, "MATCH (c:Class)-[:INCLUDES]->(m:Module) RETURN c.name AS c, m.name AS m"))
    assert rels == [("K", "Comparable"), ("K", "Enumerable")]
    assert not forced, forced


def test_external_calls_split_and_only_for_existing_callers(env):
    writer, driver, forced, backend = env
    writer.add_file_to_graph({"path": A, "repo_path": REPO, "lang": "rust",
                              "functions": [_fn("run", 10, [])]}, "fx", {})
    base = {"caller_file_path": A, "caller_line_number": 10, "full_call_name": "Lock", "args": []}
    rows = [
        {**base, "caller_name": "run", "external_type": "Mutex", "called_name": "lock", "line_number": 11, "confidence": 0.9},
        {**base, "caller_name": "run", "external_type": "Mutex", "called_name": "lock", "line_number": 12},
        {**base, "caller_name": "run", "external_type": "Arc", "called_name": "new", "line_number": 13},
        {**base, "caller_name": "ghost", "external_type": "Nowhere", "called_name": "x", "line_number": 14},
    ]
    with driver.session() as session:
        GraphWriter._write_external_calls(session, rows, "Function", 100)
    assert sorted(r["n"] for r in _q(driver, "MATCH (e:ExternalClass) RETURN e.name AS n")) == ["Arc", "Mutex"]
    calls = sorted((r["e"], r["l"], r["c"]) for r in _q(driver, "MATCH (:Function {name: 'run'})-[r:CALLS]->(e:ExternalClass) "
                                                         "RETURN e.name AS e, r.line_number AS l, r.called_name AS c"))
    assert calls == [("Arc", 13, "new"), ("Mutex", 11, "lock"), ("Mutex", 12, "lock")]
    assert not forced, forced


def test_row_folding_helpers():
    from code_visualizer.tools.indexing.persistence.writer import (
        _first_row_per_key, _last_row_per_key, _module_metadata_rows,
    )
    rows = [{"k": 1, "v": "a"}, {"k": 2, "v": "b"}, {"k": 1, "v": "c"}]
    assert _first_row_per_key(rows, lambda r: r["k"]) == [{"k": 1, "v": "a"}, {"k": 2, "v": "b"}]
    assert _last_row_per_key(rows, lambda r: r["k"]) == [{"k": 2, "v": "b"}, {"k": 1, "v": "c"}]
    imports = [{"name": "M", "lang": None, "full_import_name": "use M::a;"},
               {"name": "N", "lang": "rust", "full_import_name": "use N;"},
               {"name": "M", "lang": "rust", "full_import_name": "use M::b;"}]
    assert _module_metadata_rows(imports) == [
        {"name": "M", "lang": "rust", "full_import_name": "use M::a;"},
        {"name": "N", "lang": "rust", "full_import_name": "use N;"},
    ]


def _symbol_counts(driver):
    return {lbl: _q(driver, f"MATCH (n:{lbl}) RETURN count(n) AS c")[0]["c"] for lbl in ("Function", "Parameter", "Class")} | {
        "CONTAINS": _q(driver, "MATCH (:File)-[r:CONTAINS]->() RETURN count(r) AS c")[0]["c"],
        "HAS_PARAMETER": _q(driver, "MATCH ()-[r:HAS_PARAMETER]->() RETURN count(r) AS c")[0]["c"]}


NEW_FILE = {"path": A, "repo_path": REPO, "lang": "rust",
            "functions": [_fn("f", 10, ["a", "b"]), _fn("f", 10, ["a", "c"]), _fn("g", 20, ["a"])],
            "classes": [{"name": "K", "line_number": 1, "end_line": 30, "lang": "rust"}]}
NEW_FILE_COUNTS = {"Function": 3, "Parameter": 4, "Class": 1, "CONTAINS": 4, "HAS_PARAMETER": 5}


def test_new_file_symbols_are_created(env, monkeypatch):
    writer, driver, forced, backend = env
    import code_visualizer.tools.indexing.persistence.writer as w
    seen = []
    original = w._create_or_merge
    monkeypatch.setattr(w, "_create_or_merge", lambda *a, **k: (seen.append(a[1]), original(*a, **k))[1])
    writer.add_file_to_graph(dict(NEW_FILE), "fx", {})
    assert any("CREATE (n:Function" in q for q in seen) and any("CREATE (p:Parameter" in q for q in seen)
    assert _symbol_counts(driver) == NEW_FILE_COUNTS


def test_reindexed_file_is_merged_not_duplicated(env):
    writer, driver, forced, backend = env
    writer.add_file_to_graph(dict(NEW_FILE), "fx", {})
    writer.add_file_to_graph(dict(NEW_FILE), "fx", {})
    assert _symbol_counts(driver) == NEW_FILE_COUNTS


def test_leftover_node_falls_back_to_merge(env):
    writer, driver, forced, backend = env
    with driver.session() as s:   # a Function of the file, without its File node
        s.run("MERGE (n:Function {name: $n, path: $p, line_number: $l, occurrence_index: $o})", n="g", p=A, l=20, o=0)
    writer.add_file_to_graph(dict(NEW_FILE), "fx", {})
    assert _symbol_counts(driver) == NEW_FILE_COUNTS
