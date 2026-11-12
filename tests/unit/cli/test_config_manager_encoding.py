import builtins
from pathlib import Path

import pytest

from code_visualizer.cli import config_manager


def _require_utf8_open_for(paths, monkeypatch):
    original_open = builtins.open
    tracked = {Path(path) for path in paths}

    def open_spy(file, mode="r", *args, **kwargs):
        if Path(file) in tracked and any(flag in mode for flag in ("r", "w")):
            assert kwargs.get("encoding") == "utf-8"
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_spy)


def test_env_config_files_are_opened_with_utf8(tmp_path, monkeypatch):
    global_config = tmp_path / ".env"
    local_config = tmp_path / "local.env"
    global_config.write_text("# em dash: —\nDEFAULT_DATABASE=falkordb\n", encoding="utf-8")
    local_config.write_text("# cjk: 日本語\nSKIP_EXTERNAL_RESOLUTION=false\n", encoding="utf-8")

    monkeypatch.setattr(config_manager, "CONFIG_FILE", global_config)
    monkeypatch.setattr(config_manager, "find_local_env", lambda: local_config)
    _require_utf8_open_for([global_config, local_config], monkeypatch)

    loaded = config_manager.load_config()

    assert loaded["DEFAULT_DATABASE"] == "falkordb"
    assert loaded["SKIP_EXTERNAL_RESOLUTION"] == "false"


