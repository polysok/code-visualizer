"""UNWIND MATCH on a uid's components becomes a primary-key lookup.

`MATCH (n:Function {name: row.name, path: $file_path, line_number: …,
occurrence_index: …})` made LadybugDB scan the whole Function table for every
batch, so a full index slowed down as the graph grew (the per-file write time
tripled over a 1,800-file index). When every row holds every component, the
translator rewrites the pattern into `{uid: row.cviz_uid_n}`, the uid that the
MERGE of that node was given. Anything it cannot key exactly is left alone.
"""
from pathlib import Path

import pytest

from code_visualizer.core.database_ladybug import LadybugDBManager

pytest.importorskip("ladybug")

PARAM_REL = """
    UNWIND $batch AS row
    MATCH (fn:Function {name: row.func_name, path: $file_path, line_number: row.line_number, occurrence_index: row.occurrence_index})
    MATCH (p:Parameter {name: row.arg_name, path: $file_path, function_line_number: row.line_number})
    MERGE (fn)-[:HAS_PARAMETER]->(p)
"""


@pytest.fixture()
def driver(tmp_path: Path):
    manager = LadybugDBManager(str(tmp_path / "db"))
    yield manager.get_driver()
    manager.close_driver()


def _translate(driver, query, **params):
    with driver.session() as s:
        return s._translate_query(query, params)


def _row(**overrides):
    row = {"func_name": "f", "line_number": 10, "occurrence_index": 0, "arg_name": "a"}
    row.update(overrides)
    return row


def test_full_key_match_becomes_uid_lookup(driver):
    translated, params = _translate(driver, PARAM_REL, batch=[_row()], file_path="/r/a.py")
    assert "{uid: row.cviz_uid_fn}" in translated
    assert "{uid: row.cviz_uid_p}" in translated
    assert "line_number: row.line_number" not in translated
    row = params["batch"][0]
    assert row["cviz_uid_fn"] == "f/r/a.py100"     # name + path + line + occurrence, as 1.5b builds it
    assert row["cviz_uid_p"] == "a/r/a.py10"


@pytest.mark.parametrize("bad", [{"line_number": -1}, {"line_number": None}, {"occurrence_index": None}])
def test_unusable_component_in_any_row_keeps_the_pattern(driver, bad):
    translated, _ = _translate(driver, PARAM_REL, batch=[_row(), _row(**bad)], file_path="/r/a.py")
    assert "cviz_uid_fn" not in translated


def test_partial_or_extra_keys_are_left_alone(driver):
    q = """
        UNWIND $batch AS row
        MATCH (c:Class {name: row.class_name, path: $file_path})
        MATCH (fn:Function {name: row.f, path: $file_path, line_number: row.l, occurrence_index: row.o, lang: row.lang})
        RETURN c, fn
    """
    translated, _ = _translate(driver, q, batch=[{"class_name": "K", "f": "f", "l": 1, "o": 0, "lang": "py"}],
                               file_path="/r/a.py")
    assert "cviz_uid" not in translated


def test_merge_patterns_are_not_touched(driver):
    q = """
        UNWIND $batch AS row
        MERGE (n:Function {name: row.name, path: $file_path, line_number: row.line_number, occurrence_index: row.occurrence_index})
    """
    translated, _ = _translate(driver, q, batch=[{"name": "f", "line_number": 1, "occurrence_index": 0}],
                               file_path="/r/a.py")
    assert "cviz_uid" not in translated
    assert "uid: row.uid" in translated


def test_uid_lookup_finds_the_merged_nodes(driver):
    path = "/r/a.py"
    fns = [{"name": "f", "line_number": 10, "occurrence_index": 0}, {"name": "f", "line_number": 10, "occurrence_index": 1},
           {"name": "g", "line_number": 20, "occurrence_index": 0}]
    params = [_row(), _row(occurrence_index=1, arg_name="b"), _row(func_name="g", line_number=20)]
    with driver.session() as s:
        s.run("UNWIND $batch AS row MERGE (n:Function {name: row.name, path: $file_path, "
              "line_number: row.line_number, occurrence_index: row.occurrence_index})", batch=fns, file_path=path)
        s.run("UNWIND $batch AS row MERGE (p:Parameter {name: row.arg_name, path: $file_path, "
              "function_line_number: row.line_number})", batch=params, file_path=path)
        s.run(PARAM_REL, batch=params, file_path=path)
        edges = sorted((r["o"], r["fn"], r["p"]) for r in s.run(
            "MATCH (f:Function)-[:HAS_PARAMETER]->(p:Parameter) "
            "RETURN f.occurrence_index AS o, f.name AS fn, p.name AS p").data())
    assert edges == [(0, "f", "a"), (0, "g", "a"), (1, "f", "b")]
