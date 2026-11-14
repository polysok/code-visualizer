"""load_config() is cached, and the cache follows every input it reads.

It runs on every log line of an index (tens of thousands of times), so it is
cached; a stale value would be worse than a slow one, so each input that can
change the result (the environment, the working directory, the global and
project .env files) must invalidate the cache.
"""

import os
import time

import pytest

import code_visualizer.cli.config_manager as cm


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cm, "CONFIG_DIR", home)
    monkeypatch.setattr(cm, "CONFIG_FILE", home / ".env")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("ENABLE_APP_LOGS", raising=False)
    monkeypatch.delenv("CVIZ_IGNORE_PROJECT_ENV", raising=False)
    cm.invalidate_config_cache()
    reads = []
    original = cm._load_config_uncached

    def counting():
        reads.append(1)
        return original()

    monkeypatch.setattr(cm, "_load_config_uncached", counting)
    yield home, repo, reads
    cm.invalidate_config_cache()


def _rewrite(path, text):
    # Same size on purpose: only the mtime / inode may tell the change apart.
    time.sleep(0.01)
    path.write_text(text)


def test_repeated_loads_read_once(cfg):
    _, _, reads = cfg
    for _ in range(50):
        cm.get_config_value("ENABLE_APP_LOGS")
    assert len(reads) == 1


def test_returned_dict_is_a_copy(cfg):
    config = cm.load_config()
    config["ENABLE_APP_LOGS"] = "mutated"
    assert cm.load_config().get("ENABLE_APP_LOGS") != "mutated"


def test_environment_change_is_seen(cfg, monkeypatch):
    assert cm.get_config_value("ENABLE_APP_LOGS") != "DEBUG"
    monkeypatch.setenv("ENABLE_APP_LOGS", "DEBUG")
    assert cm.get_config_value("ENABLE_APP_LOGS") == "DEBUG"
    monkeypatch.delenv("ENABLE_APP_LOGS")
    assert cm.get_config_value("ENABLE_APP_LOGS") != "DEBUG"


def test_global_env_file_change_is_seen(cfg):
    home, _, _ = cfg
    (home / ".env").write_text("ENABLE_APP_LOGS=ERROR\n")
    assert cm.get_config_value("ENABLE_APP_LOGS") == "ERROR"
    _rewrite(home / ".env", "ENABLE_APP_LOGS=DEBUG\n")
    assert cm.get_config_value("ENABLE_APP_LOGS") == "DEBUG"


def test_project_env_file_change_is_seen(cfg):
    _, repo, _ = cfg
    assert cm.get_config_value("ENABLE_APP_LOGS") != "ERROR"
    (repo / ".code-visualizer").mkdir()
    (repo / ".code-visualizer" / ".env").write_text("ENABLE_APP_LOGS=ERROR\n")
    assert cm.get_config_value("ENABLE_APP_LOGS") == "ERROR"
    (repo / ".code-visualizer" / ".env").unlink()
    assert cm.get_config_value("ENABLE_APP_LOGS") != "ERROR"


def test_working_directory_change_is_seen(cfg, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("PARALLEL_WORKERS=7\n")
    monkeypatch.delenv("PARALLEL_WORKERS", raising=False)
    assert cm.get_config_value("PARALLEL_WORKERS") != "7"
    monkeypatch.chdir(other)
    assert cm.get_config_value("PARALLEL_WORKERS") == "7"


def test_save_config_invalidates(cfg):
    home, _, _ = cfg
    config = cm.load_config()
    config["ENABLE_APP_LOGS"] = "WARNING"
    cm.save_config(config)
    assert cm.get_config_value("ENABLE_APP_LOGS") == "WARNING"


def test_patched_config_file_is_seen(cfg, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text("ENABLE_APP_LOGS=ERROR\n")
    cm.get_config_value("ENABLE_APP_LOGS")
    monkeypatch.setattr(cm, "CONFIG_FILE", elsewhere)
    assert cm.get_config_value("ENABLE_APP_LOGS") == "ERROR"
