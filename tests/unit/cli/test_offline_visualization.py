"""Regression tests for the offline visualization fallback."""
import inspect
from unittest.mock import MagicMock

import pytest

from code_visualizer.cli import cli_helpers


class _Session:
    def __init__(self, records):
        self._records = records

    def run(self, query, **params):
        return iter(self._records)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Record(list):
    def values(self):
        return list(self)


def _db_manager(records):
    manager = MagicMock()
    manager.get_driver.return_value.session.return_value = _Session(records)
    return manager


# --- Kùzu / Ladybug shape: plain dicts with _id / _label / _src / _dst --------

KUZU_RECORDS = [
    _Record([
        {"_id": "0:1", "_label": "File", "path": "/repo/a.py", "name": "a.py"},
        {"_src": "0:1", "_dst": "0:2", "_label": "CONTAINS"},
        {"_id": "0:2", "_label": "Function", "name": "alpha", "path": "/repo/a.py",
         "line_number": 3},
    ])
]

# --- Ladybug shape: same dicts but with UPPERCASE internal keys (#1458) ------

LADYBUG_RECORDS = [
    _Record([
        {"_ID": "0:1", "_LABEL": "File", "path": "/repo/a.py", "name": "a.py"},
        {"_SRC": "0:1", "_DST": "0:2", "_LABEL": "CONTAINS"},
        {"_ID": "0:2", "_LABEL": "Function", "name": "alpha", "path": "/repo/a.py",
         "line_number": 3},
    ])
]

@pytest.mark.parametrize(
    "records,label",
    [
        (KUZU_RECORDS, "kuzu-style dicts"),
        (LADYBUG_RECORDS, "ladybug-style uppercase dicts"),
    ],
)
def test_offline_renderer_handles_both_driver_shapes(records, label, tmp_path, monkeypatch):
    """Kùzu returns plain dicts carrying `_label`/`_src`/`_dst` (lowercase);
    LadybugDB returns the same plain-dict shape but UPPERCASE
    (`_LABEL`/`_SRC`/`_DST`). Neo4j returns driver objects with
    `.labels`/`.type` that are dict-convertible. FalkorDB also carries
    `.labels` but is NOT dict-convertible (properties live in `.properties`),
    and its edges use `.src_node`/`.dest_node`/`.relation` rather than
    `.start_node`/`.end_node`/`.type`. The renderer must understand all four
    shapes or it crashes (FalkorDB) or silently produces an empty graph
    (LadybugDB, issue #1458)."""
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    out = tmp_path / "graph.html"

    cli_helpers._render_offline_visualization(
        _db_manager(records), repo_path="/repo", output_path=str(out)
    )

    assert out.exists(), f"no HTML rendered for {label}"
    html = out.read_text(encoding="utf-8")
    assert "alpha" in html, f"node data missing for {label}"
    assert "CONTAINS" in html, f"edge data missing for {label}"


def test_offline_renderer_is_self_contained(tmp_path, monkeypatch):
    """The point of the fallback is that it needs no bundled assets, so the
    page must not reference any external resource."""
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    out = tmp_path / "graph.html"

    cli_helpers._render_offline_visualization(
        _db_manager(KUZU_RECORDS), output_path=str(out)
    )

    html = out.read_text(encoding="utf-8")
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://" not in html.replace("https://www.w3.org", "")


def test_offline_renderer_reports_an_empty_graph(monkeypatch, tmp_path):
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)

    with pytest.raises(Exception):  # typer.Exit
        cli_helpers._render_offline_visualization(
            _db_manager([]), output_path=str(tmp_path / "x.html")
        )


def test_visualize_uses_the_built_in_renderer():
    source = inspect.getsource(cli_helpers.visualize_helper)
    assert "_render_offline_visualization" in source
