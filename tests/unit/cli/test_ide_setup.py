"""`cviz ide zed` / `cviz ide jetbrains` (cli/ide_setup.py)."""
import json
import re

import pytest

from code_visualizer.cli import ide_setup


def _tasks(path):
    text = ide_setup._strip_jsonc(path.read_text(encoding="utf-8"))
    return json.loads(re.sub(r",(\s*[\]}])", r"\1", text))  # Zed accepts trailing commas


def test_zed_creates_tasks_file(tmp_path):
    msg = ide_setup.install_zed_task(tmp_path, "cviz-open", [])
    tasks_file = tmp_path / ".zed" / "tasks.json"
    assert msg.startswith("created")
    (task,) = _tasks(tasks_file)
    assert task["label"] == ide_setup.ZED_TASK_LABEL
    assert task["command"] == "cviz-open"
    assert task["args"] == ["$ZED_FILE:$ZED_ROW", "--ide", "zed"]


def test_zed_appends_and_keeps_comments_then_is_idempotent(tmp_path):
    tasks_file = tmp_path / ".zed" / "tasks.json"
    tasks_file.parent.mkdir()
    tasks_file.write_text(
        '// team tasks\n[\n  {\n    "label": "build", // keep me\n    "command": "make",\n  },\n]\n',
        encoding="utf-8",
    )
    assert ide_setup.install_zed_task(tmp_path, "cviz", ["open"]).startswith("added")
    text = tasks_file.read_text(encoding="utf-8")
    assert "// team tasks" in text and "// keep me" in text
    labels = [t["label"] for t in _tasks(tasks_file)]
    assert labels == ["build", ide_setup.ZED_TASK_LABEL]
    assert _tasks(tasks_file)[1]["args"][0] == "open"
    assert ide_setup.install_zed_task(tmp_path, "cviz", ["open"]).startswith("already present")
    assert tasks_file.read_text(encoding="utf-8") == text


def test_zed_empty_array_and_invalid_file(tmp_path):
    tasks_file = tmp_path / ".zed" / "tasks.json"
    tasks_file.parent.mkdir()
    tasks_file.write_text("[]\n", encoding="utf-8")
    ide_setup.install_zed_task(tmp_path, "cviz-open", [])
    assert len(_tasks(tasks_file)) == 1
    tasks_file.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        ide_setup.install_zed_task(tmp_path, "cviz-open", [])


def test_strip_jsonc_keeps_slashes_inside_strings():
    assert json.loads(ide_setup._strip_jsonc('{"url": "http://x//y" /* c */} // end')) == {"url": "http://x//y"}


def test_jetbrains_dirs_and_tool(tmp_path, monkeypatch):
    for name in ("Rider2025.2", "IntelliJIdea2024.3", "consentOptions", "Rider2025.2-backup"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(ide_setup, "jetbrains_config_roots", lambda: [tmp_path])
    dirs = ide_setup.jetbrains_config_dirs()
    assert [d.name for d in dirs] == ["IntelliJIdea2024.3", "Rider2025.2"]
    assert [d.name for d in ide_setup.jetbrains_config_dirs("rider")] == ["Rider2025.2"]

    rider = tmp_path / "Rider2025.2"
    assert ide_setup.install_jetbrains_tool(rider, "/opt/cviz tools/cviz-open", [], dry_run=True).startswith("would")
    assert not (rider / "tools").exists()
    assert ide_setup.install_jetbrains_tool(rider, "/opt/cviz tools/cviz-open", []).startswith("wrote")
    xml = (rider / "tools" / "CViz.xml").read_text(encoding="utf-8")
    assert 'value="/opt/cviz tools/cviz-open"' in xml
    assert '"$FilePath$:$LineNumber$" --ide rider' in xml.replace("&quot;", '"')
    assert ide_setup.install_jetbrains_tool(rider, "/opt/cviz tools/cviz-open", []).startswith("up to date")

    idea = tmp_path / "IntelliJIdea2024.3"
    ide_setup.install_jetbrains_tool(idea, "/usr/bin/cviz", ["open"])
    assert "open " in (idea / "tools" / "CViz.xml").read_text(encoding="utf-8")
    assert "--ide idea" in (idea / "tools" / "CViz.xml").read_text(encoding="utf-8")
