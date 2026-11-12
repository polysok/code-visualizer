"""Query-shape and argument-plumbing defects found in the 0.5.x audit.

Each test pins one defect that produced a plausible-looking but wrong answer
rather than an error, which is the class of bug an agent cannot detect.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from code_visualizer.tools.code_finder import CodeFinder


# --------------------------------------------------------------- #1533
def test_injection_count_counts_relationships_not_rows():
    """`count(*)` after an unmatched OPTIONAL MATCH still counts one null row,
    so every Spring bean reported at least one injector."""
    import inspect

    from code_visualizer.tools.query_tool_languages import java_toolkit

    src = inspect.getsource(java_toolkit)
    assert "count(inj) AS injection_count" in src
    assert "count(*) AS injection_count" not in src


# --------------------------------------------------------------- #1530
class _Recorder:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None

    def session(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        self.last_query = query
        res = MagicMock()
        res.data.return_value = self._rows
        return res


def _finder_with(rows):
    dbm = MagicMock()
    rec = _Recorder(rows)
    dbm.get_driver.return_value = rec
    dbm.get_backend_type.return_value = "neo4j"
    return CodeFinder(dbm)


def test_find_callers_keeps_per_row_target_when_definitions_differ():
    """Without `context`, callers of a common name legitimately point at
    different definitions; hoisting the first row's path reported every caller
    as calling whichever file sorted first."""
    rows = [
        {"caller": "a", "target_file_path": "/repo/models/user.py"},
        {"caller": "b", "target_file_path": "/repo/models/order.py"},
    ]
    finder = _finder_with(rows)
    out = finder.analyze_code_relationships("find_callers", "save")

    assert out["target_file_path"] is None, "must not claim a single target"
    paths = {r.get("target_file_path") for r in out["results"]}
    assert paths == {"/repo/models/user.py", "/repo/models/order.py"}
    assert "note" in out and "2 files" in out["note"]


def test_find_callers_still_hoists_when_all_rows_agree():
    """The payload-slimming behaviour is kept for the unambiguous case."""
    rows = [
        {"caller": "a", "target_file_path": "/repo/models/user.py"},
        {"caller": "b", "target_file_path": "/repo/models/user.py"},
    ]
    finder = _finder_with(rows)
    out = finder.analyze_code_relationships("find_callers", "save")

    assert out["target_file_path"] == "/repo/models/user.py"
    assert all("target_file_path" not in r for r in out["results"])
    assert "note" not in out


# --------------------------------------------------------------- #1531
# --------------------------------------------------------------- #1558
