"""Which files a staged first index takes first, and its setting."""
from pathlib import Path

import pytest

from code_visualizer.tools.indexing.discovery import prioritize_files

ROOT = Path("/repo")


def _order(*rels):
    return [p.relative_to(ROOT).as_posix() for p in prioritize_files([ROOT / r for r in rels], ROOT)]


def test_code_before_tests_before_generated_then_shallow_first():
    assert _order(
        "db/migrations/0001_initial.py", "src/app/deep/service.py", "tests/test_app.py",
        "main.py", "src/App.Designer.cs", "src/app.py", "web/app.spec.ts", "src/AppTests.cs",
    ) == [
        "main.py", "src/app.py", "src/app/deep/service.py",
        "src/AppTests.cs", "tests/test_app.py", "web/app.spec.ts",
        "src/App.Designer.cs", "db/migrations/0001_initial.py",
    ]


def test_order_is_deterministic_for_ties():
    assert _order("b.py", "a.py", "c/d.py") == ["a.py", "b.py", "c/d.py"]


@pytest.mark.parametrize("raw, expected", [("1000", 1000), ("25", 25), ("0", None), ("unlimited", None),
                                           ("", None), ("-3", None), ("abc", None)])
def test_first_pass_setting(monkeypatch, raw, expected):
    import code_visualizer.cli.cli_helpers as helpers
    monkeypatch.setattr(helpers, "get_config_value", lambda key: raw if key == "GRAPH_FIRST_PASS_FILES" else None)
    assert helpers._graph_first_pass_files() == expected


@pytest.mark.parametrize("value, ok", [("1000", True), ("0", True), ("unlimited", True), ("-1", False), ("x", False)])
def test_first_pass_setting_validation(value, ok):
    from code_visualizer.cli.config_manager import validate_config_value
    assert validate_config_value("GRAPH_FIRST_PASS_FILES", value)[0] is ok
