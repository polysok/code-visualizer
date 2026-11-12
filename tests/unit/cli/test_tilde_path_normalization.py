import json
import os
from pathlib import Path

from code_visualizer.cli import config_manager
from code_visualizer.cli import setup_wizard


def _set_home_env(monkeypatch, tmp_path):
    """Set HOME (and USERPROFILE on Windows) so os.path.expanduser('~') resolves to tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_normalize_config_path_expands_home_and_makes_absolute(tmp_path, monkeypatch):
    _set_home_env(monkeypatch, tmp_path)
    normalized = config_manager.normalize_config_path("~/.code-visualizer/global/db/falkordb", absolute=True)
    assert normalized == str((tmp_path / ".code-visualizer" / "global" / "db" / "falkordb").resolve())


def test_configure_mcp_client_writes_expanded_paths(tmp_path, monkeypatch):
    _set_home_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    fake_config = {
        "LADYBUGDB_PATH": "~/.code-visualizer/global/db/ladybugdb",
        "DEBUG_LOG_PATH": "~/mcp_debug.log",
        "LOG_FILE_PATH": "~/.code-visualizer/logs/cviz.log",
    }

    monkeypatch.setattr(config_manager, "load_config", lambda: fake_config)
    monkeypatch.setattr(setup_wizard, "_configure_ide", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "/usr/local/bin/cviz" if name == "cviz" else None)

    setup_wizard.configure_mcp_client()

    mcp_json = tmp_path / "mcp.json"
    assert mcp_json.exists()
    data = json.loads(mcp_json.read_text(encoding="utf-8"))
    env = data["mcpServers"]["code-visualizer"]["env"]

    assert env["LADYBUGDB_PATH"] == str((tmp_path / ".code-visualizer" / "global" / "db" / "ladybugdb").resolve())
    assert env["DEBUG_LOG_PATH"] == str((tmp_path / "mcp_debug.log").resolve())
    assert env["LOG_FILE_PATH"] == str((tmp_path / ".code-visualizer" / "logs" / "cviz.log").resolve())
