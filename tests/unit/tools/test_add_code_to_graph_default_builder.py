"""add_code_to_graph indexes through the default GraphBuilder (LadybugDB keeps a single graph)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_visualizer.tools.handlers.indexing_handlers import add_code_to_graph


def _base_builder(backend: str):
    gb = MagicMock()
    gb.db_manager.get_backend_type.return_value = backend
    gb.estimate_processing_time.return_value = (3, 1.0)
    return gb


def test_no_graph_name_keeps_default_builder(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CVIZ_ALLOWED_ROOTS", str(tmp_path))
    gb = _base_builder("kuzudb")
    gb.build_graph_from_path_async.return_value = MagicMock()
    job_manager = MagicMock()
    job_manager.create_job.return_value = "job-2"

    with patch("code_visualizer.tools.handlers.indexing_handlers.asyncio"):
        result = add_code_to_graph(
            gb, job_manager, MagicMock(), lambda: {"repositories": []},
            path=str(tmp_path),
        )

    assert result.get("success") is True, result
    assert "graph_name" not in result
    gb.build_graph_from_path_async.assert_called_once()
