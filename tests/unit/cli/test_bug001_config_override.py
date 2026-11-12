"""
Test for BUG-001: Config Override Issue
Ensures that local .code-visualizer/.env does not override global config in global/named mode.
"""

import os
from pathlib import Path
from unittest.mock import patch
import pytest

from code_visualizer.cli import config_manager


def test_per_repo_mode_uses_local_dotenv(tmp_path, monkeypatch):
    """
    In per-repo mode, local .code-visualizer/.env SHOULD override global config.
    This is the intended behavior for per-repo isolation.
    """
    # Setup: Create a fake HOME with global config
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    
    global_cviz_dir = fake_home / ".code-visualizer"
    global_cviz_dir.mkdir()
    
    # Create global config
    global_env = global_cviz_dir / ".env"
    global_env.write_text("DEFAULT_DATABASE=neo4j\n")
    
    # Create a repo with its own config
    repo_dir = fake_home / "my_project"  # Inside home so relative_to works
    repo_dir.mkdir()
    repo_cviz = repo_dir / ".code-visualizer"
    repo_cviz.mkdir()
    
    repo_env = repo_cviz / ".env"
    repo_env.write_text("DEFAULT_DATABASE=kuzudb\n")
    
    # Create config.yaml in PER-REPO mode
    config_yaml = global_cviz_dir / "config.yaml"
    config_yaml.write_text("version: 1\nmode: per-repo\n")
    
    # Simulate being in the repo directory
    with patch.object(Path, "cwd", return_value=repo_dir):
        with patch.object(config_manager, "CONFIG_DIR", global_cviz_dir):
            with patch.object(config_manager, "CONFIG_FILE", global_env):
                with patch.object(config_manager, "CONTEXT_CONFIG_FILE", config_yaml):
                    config = config_manager.load_config()
    
    # ASSERTION: In per-repo mode, local config should win
    assert config["DEFAULT_DATABASE"] == "kuzudb", \
        "Per-repo mode should use local .code-visualizer/.env config"


def test_env_vars_override_everything(tmp_path, monkeypatch):
    """
    Environment variables should always have highest priority, regardless of mode.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    
    global_cviz_dir = fake_home / ".code-visualizer"
    global_cviz_dir.mkdir()
    
    global_env = global_cviz_dir / ".env"
    global_env.write_text("DEFAULT_DATABASE=falkordb\n")
    
    # Set environment variable
    monkeypatch.setenv("DEFAULT_DATABASE", "neo4j")
    
    with patch.object(config_manager, "CONFIG_DIR", global_cviz_dir):
        with patch.object(config_manager, "CONFIG_FILE", global_env):
            config = config_manager.load_config()
    
    # Environment variable should win
    assert config["DEFAULT_DATABASE"] == "neo4j", \
        "Environment variables should override all config files"


def test_cviz_load_project_env_forces_load(tmp_path, monkeypatch):
    """
    CVIZ_LOAD_PROJECT_ENV=1 should force loading project .env even in global mode.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CVIZ_LOAD_PROJECT_ENV", "1")
    
    global_cviz_dir = fake_home / ".code-visualizer"
    global_cviz_dir.mkdir()
    
    global_env = global_cviz_dir / ".env"
    global_env.write_text("DEFAULT_DATABASE=neo4j\n")
    
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo_cviz = repo_dir / ".code-visualizer"
    repo_cviz.mkdir()
    
    repo_env = repo_cviz / ".env"
    repo_env.write_text("DEFAULT_DATABASE=kuzudb\n")
    
    config_yaml = global_cviz_dir / "config.yaml"
    config_yaml.write_text("version: 1\nmode: global\n")
    
    with patch.object(Path, "cwd", return_value=repo_dir):
        with patch.object(config_manager, "CONFIG_DIR", global_cviz_dir):
            with patch.object(config_manager, "CONFIG_FILE", global_env):
                with patch.object(config_manager, "CONTEXT_CONFIG_FILE", config_yaml):
                    # Force flag should allow project env to load
                    assert config_manager.should_apply_project_dotenv() is True
                    config = config_manager.load_config()
    
    # With force flag, repo config should be loaded
    # But DB_PATH_ENV_KEYS should still be protected
    assert config["DEFAULT_DATABASE"] == "kuzudb", \
        "CVIZ_LOAD_PROJECT_ENV=1 should allow project config to override"


def test_db_path_keys_never_override_in_global_mode(tmp_path, monkeypatch):
    """
    CRITICAL: DB path keys (FALKORDB_PATH, KUZUDB_PATH, etc.) should NEVER
    be overridden by local .env files, even with CVIZ_LOAD_PROJECT_ENV=1.
    This prevents database corruption and silent data mixing.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CVIZ_LOAD_PROJECT_ENV", "1")  # Force load
    
    global_cviz_dir = fake_home / ".code-visualizer"
    global_cviz_dir.mkdir()
    
    global_env = global_cviz_dir / ".env"
    global_env.write_text(
        "FALKORDB_PATH=/home/user/.code-visualizer/global/db/falkordb\n"
        "KUZUDB_PATH=/home/user/.code-visualizer/global/db/kuzudb\n"
    )
    
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo_cviz = repo_dir / ".code-visualizer"
    repo_cviz.mkdir()
    
    # Malicious or accidental override attempt
    repo_env = repo_cviz / ".env"
    repo_env.write_text(
        "FALKORDB_PATH=/tmp/evil_db\n"
        "KUZUDB_PATH=/tmp/another_evil_db\n"
    )
    
    config_yaml = global_cviz_dir / "config.yaml"
    config_yaml.write_text("version: 1\nmode: global\n")
    
    with patch.object(Path, "cwd", return_value=repo_dir):
        with patch.object(config_manager, "CONFIG_DIR", global_cviz_dir):
            with patch.object(config_manager, "CONFIG_FILE", global_env):
                with patch.object(config_manager, "CONTEXT_CONFIG_FILE", config_yaml):
                    config = config_manager.load_config()
    
    # CRITICAL SECURITY CHECK: DB paths should remain global, never overridden
    assert "/tmp/evil_db" not in config["FALKORDB_PATH"], \
        "BUG-001 FIX: FALKORDB_PATH must never be overridden by local .env"
    
    assert "/tmp/another_evil_db" not in config["KUZUDB_PATH"], \
        "BUG-001 FIX: KUZUDB_PATH must never be overridden by local .env"
    
    assert "global/db/falkordb" in config["FALKORDB_PATH"], \
        "Global DB path should be preserved"
