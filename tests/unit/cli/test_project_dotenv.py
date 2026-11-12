"""Tests for per-repo vs global project .env loading (BUG-001)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from code_visualizer.cli import config_manager


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CVIZ_LOAD_PROJECT_ENV", raising=False)
    monkeypatch.delenv("CVIZ_IGNORE_PROJECT_ENV", raising=False)
    return tmp_path


def test_per_repo_mode_loads_project_dotenv(isolated_home):
    repo = isolated_home / "myrepo"
    cviz_dir = repo / ".code-visualizer"
    cviz_dir.mkdir(parents=True)
    (cviz_dir / ".env").write_text("DEFAULT_DATABASE=kuzudb\n", encoding="utf-8")

    with patch.object(Path, "cwd", return_value=repo):
        assert config_manager.should_apply_project_dotenv() is True


