"""#1710: consecutive MATCH clauses inside UNWIND batches merge to comma-form.

LadybugDB's planner silently mis-binds
`UNWIND $b AS row MATCH (a:File {path: row.p}) MATCH (b:Function {…})`: after
a primary-key lookup, the next MATCH's predicates on row fields are lost, so
a row binds the nodes of other rows (or none), no error is raised, and the
MERGEs downstream write wrong or missing edges (every File-sourced CALLS edge
and five Struct CONTAINS edges missing on ladybug only, run after run).

The comma form `MATCH (a {…}), (b {…})` is semantically identical for
non-optional patterns and both engines plan it correctly, so the embedded
translation layer rewrites adjacent MATCH clauses in UNWIND queries. A chain
of primary-key lookups only binds correctly chained, and the comma form makes
it a join whose cost grows with the tables, so such a chain stays chained
(CVIZ_LADYBUG_COMMA_MATCH=true joins it too, as before).
"""
from pathlib import Path

import pytest

from code_visualizer.core.database_ladybug import LadybugDBManager

kuzu = pytest.importorskip("ladybug")


@pytest.fixture()
def driver(tmp_path: Path):
    manager = LadybugDBManager(str(tmp_path / "db"))
    yield manager.get_driver()
    manager.close_driver()


def _translate(driver, query, **params):
    with driver.session() as s:
        translated, _ = s._translate_query(query, params)
    return translated


UNWIND_DOUBLE_MATCH = """
    UNWIND $batch AS row
    MATCH (caller:File {path: row.caller_file_path})
    MATCH (called:Function {name: row.called_name, path: row.called_file_path})
    WHERE row.called_line_number <= 0 OR called.line_number = row.called_line_number
    MERGE (caller)-[c:CALLS {line_number: row.line_number}]->(called)
"""


def test_adjacent_matches_merge_to_comma_form(driver):
    translated = _translate(driver, UNWIND_DOUBLE_MATCH, batch=[{"caller_file_path": "/f.py"}])
    assert "}), " in translated.replace("\n", " ") or "}) , " in translated
    # exactly one MATCH keyword must remain
    assert translated.count("MATCH") == 1, translated


def test_triple_match_chain_fully_merges(driver):
    q = (
        "UNWIND $batch AS row "
        "MATCH (a:File {path: row.p}) "
        "MATCH (b:Function {name: row.n}) "
        "MATCH (c:Class {name: row.c}) "
        "RETURN count(*)"
    )
    translated = _translate(driver, q, batch=[{"p": "/f.py"}])
    assert translated.count("MATCH") == 1, translated


def test_where_between_matches_blocks_the_merge(driver):
    # Merging across an interposed WHERE would change which clause the
    # predicate binds to — that shape must stay untouched.
    q = (
        "UNWIND $batch AS row "
        "MATCH (outer:Function {name: row.outer}) "
        "WHERE row.outer_line < 0 OR outer.line_number = row.outer_line "
        "MATCH (inner:Function {name: row.inner_name}) "
        "MERGE (outer)-[:CONTAINS]->(inner)"
    )
    translated = _translate(driver, q, batch=[{"outer": "x"}])
    assert translated.count("MATCH") == 2, translated


def test_optional_match_is_never_merged(driver):
    q = (
        "UNWIND $batch AS row "
        "MATCH (a:File {path: row.p}) "
        "OPTIONAL MATCH (b:Function {name: row.n}) "
        "RETURN count(*)"
    )
    translated = _translate(driver, q, batch=[{"p": "/f.py"}])
    assert "OPTIONAL MATCH" in translated, translated


def test_non_unwind_queries_stay_untouched(driver):
    q = (
        "MATCH (a:File {path: $p}) "
        "MATCH (b:Function {name: $n}) "
        "RETURN count(*)"
    )
    translated = _translate(driver, q, p="/f.py", n="x")
    assert translated.count("MATCH") == 2, translated


class TestLadybugRowReplay:
    """LadybugDB state-dependently drops SOME rows of a batched rel-only
    UNWIND MERGE (no error raised), and on CI the drop survives identical
    re-execution. The compat layer therefore replays every row of such
    batches as a single-row batch after the batched pass — idempotent MERGE
    makes the union safe — on ladybug only, when CVIZ_LADYBUG_ROW_REPLAY is
    set (off by default: no longer reproduces with LadybugDB 0.19.1)."""

    @pytest.fixture(autouse=True)
    def _replay_on(self, monkeypatch):
        monkeypatch.setenv("CVIZ_LADYBUG_ROW_REPLAY", "true")

    REL_MERGE = (
        "UNWIND $batch AS row "
        "MATCH (a:Class {name: row.c}) "
        "MATCH (b:ExternalClass {name: row.p}) "
        "MERGE (a)-[r:INHERITS]->(b)"
    )
    THREE_ROWS = [{"c": "A", "p": "P"}, {"c": "B", "p": "X"}, {"c": "C", "p": "P"}]

    def _run_counting(self, backend_id, query, batch):
        from unittest.mock import MagicMock
        from code_visualizer.core.database_embedded_kuzu import EmbeddedSessionWrapper

        conn = MagicMock()
        session = EmbeddedSessionWrapper(conn, backend_id=backend_id)
        session.run(query, batch=batch)
        return conn.execute

    def test_ladybug_replays_each_row_after_the_batch(self):
        execute = self._run_counting("ladybugdb", self.REL_MERGE, self.THREE_ROWS)
        # one batched pass + one single-row pass per row
        assert execute.call_count == 1 + len(self.THREE_ROWS)
        single_batches = [c.args[1]["batch"] for c in execute.call_args_list[1:]]
        assert single_batches == [[r] for r in self.THREE_ROWS]

    def test_kuzu_executes_rel_merge_batch_once(self):
        execute = self._run_counting("kuzudb", self.REL_MERGE, self.THREE_ROWS)
        assert execute.call_count == 1

    def test_ladybug_single_row_batches_execute_once(self):
        execute = self._run_counting("ladybugdb", self.REL_MERGE, [self.THREE_ROWS[0]])
        assert execute.call_count == 1

    def test_ladybug_read_queries_execute_once(self):
        q = "UNWIND $batch AS row MATCH (a:Class {name: row.c}) RETURN count(a)"
        execute = self._run_counting("ladybugdb", q, self.THREE_ROWS)
        assert execute.call_count == 1

    def test_ladybug_batch_runs_once_by_default(self, monkeypatch):
        monkeypatch.delenv("CVIZ_LADYBUG_ROW_REPLAY")
        execute = self._run_counting("ladybugdb", self.REL_MERGE, self.THREE_ROWS)
        assert execute.call_count == 1

    def test_failing_single_row_never_propagates(self):
        """A raising single-row replay must be swallowed: the batched pass
        already wrote its edges, and an escaping exception aborts the
        writer's remaining batches (that cost ~51 CONTAINS edges on CI)."""
        from unittest.mock import MagicMock
        from code_visualizer.core.database_embedded_kuzu import EmbeddedSessionWrapper

        conn = MagicMock()
        calls = {"n": 0}

        def _explode_on_singles(*a, **k):
            calls["n"] += 1
            if calls["n"] > 1:  # batched pass succeeds, every single fails
                raise RuntimeError("transient engine error")
            return MagicMock()

        conn.execute.side_effect = _explode_on_singles
        session = EmbeddedSessionWrapper(conn, backend_id="ladybugdb")
        session.run(self.REL_MERGE, batch=self.THREE_ROWS)  # must not raise
        assert calls["n"] == 1 + len(self.THREE_ROWS)


def test_merged_form_still_binds_rows_on_kuzu(driver):
    """End-to-end on the real engine: the rewritten comma-form must produce
    the same edges the two-MATCH form produced on kuzu before the rewrite."""
    with driver.session() as s:
        for name in ("A", "B"):
            s.run(
                "MERGE (c:Class {name: $n, path: $p, line_number: $ln, occurrence_index: $oi})",
                n=name, p="/f.py", ln=1, oi=0,
            )
            s.run("MERGE (e:ExternalClass {name: $n})", n=name + "_ext")
        s.run(
            """
            UNWIND $batch AS row
            MATCH (child:Class {name: row.c, path: '/f.py'})
            MATCH (parent:ExternalClass {name: row.p})
            MERGE (child)-[r:INHERITS]->(parent)
            """,
            batch=[{"c": "A", "p": "A_ext"}, {"c": "B", "p": "B_ext"}],
        )
        got = sorted(
            (r["c"], r["p"]) for r in s.run(
                "MATCH (c:Class)-[:INHERITS]->(p:ExternalClass) "
                "RETURN c.name AS c, p.name AS p"
            ).data()
        )
    assert got == [("A", "A_ext"), ("B", "B_ext")], got


def test_key_lookup_chain_stays_chained(driver, monkeypatch):
    monkeypatch.delenv("CVIZ_LADYBUG_COMMA_MATCH", raising=False)
    q = (
        "UNWIND $batch AS row "
        "MATCH (caller:Function {uid: row.caller_key}) "
        "MATCH (called:File {path: row.called_path}) "
        "MERGE (caller)-[:CALLS {line_number: row.line_number}]->(called)"
    )
    translated = _translate(driver, q, batch=[{"caller_key": "k", "called_path": "/f.py", "line_number": 1}])
    assert translated.count("MATCH") == 2, translated


def test_key_lookup_chain_is_joined_when_asked(driver, monkeypatch):
    monkeypatch.setenv("CVIZ_LADYBUG_COMMA_MATCH", "true")
    q = (
        "UNWIND $batch AS row "
        "MATCH (caller:Function {uid: row.caller_key}) "
        "MATCH (called:File {path: row.called_path}) "
        "RETURN count(*)"
    )
    translated = _translate(driver, q, batch=[{"caller_key": "k", "called_path": "/f.py"}])
    assert translated.count("MATCH") == 1, translated


def test_mixed_chain_is_joined_and_binds_rows(driver, monkeypatch):
    """The #1710 shape on the real engine: a File found by its path, then a
    Function by name and path. Chained, the row calling g also bound f."""
    monkeypatch.delenv("CVIZ_LADYBUG_COMMA_MATCH", raising=False)
    with driver.session() as s:
        for path in ("/a.py", "/b.py"):
            s.run("MERGE (f:File {path: $p}) SET f.name = $p", p=path)
        for name, line in (("f", 3), ("g", 7)):
            s.run("MERGE (fn:Function {name: $n, path: $p, line_number: $l, occurrence_index: $o})",
                  n=name, p="/b.py", l=line, o=0)
        s.run(
            UNWIND_DOUBLE_MATCH,
            batch=[
                {"caller_file_path": "/a.py", "called_name": "f", "called_file_path": "/b.py",
                 "called_line_number": 3, "line_number": 1},
                {"caller_file_path": "/a.py", "called_name": "g", "called_file_path": "/b.py",
                 "called_line_number": 0, "line_number": 2},
                {"caller_file_path": "/b.py", "called_name": "f", "called_file_path": "/b.py",
                 "called_line_number": 3, "line_number": 9},
            ],
        )
        got = sorted((r["f"], r["n"], r["l"]) for r in s.run(
            "MATCH (f:File)-[c:CALLS]->(fn:Function) RETURN f.path AS f, fn.name AS n, c.line_number AS l"
        ).data())
    assert got == [("/a.py", "f", 1), ("/a.py", "g", 2), ("/b.py", "f", 9)], got
