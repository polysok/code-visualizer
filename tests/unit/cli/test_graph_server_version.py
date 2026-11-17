"""cviz graph and a server of the repository that already runs another version."""
import io
import os
import subprocess

import pytest
from rich.console import Console

from code_visualizer.cli import cli_helpers, server_registry


@pytest.fixture
def running(monkeypatch, tmp_path):
    """A server of the repository runs with cviz 1.9.0; this cviz is 0.9.9."""
    out = io.StringIO()
    monkeypatch.setattr(cli_helpers, "console", Console(file=out, width=60))  # narrower than the line: it must not wrap
    monkeypatch.setattr(server_registry, "server_for", lambda path: "http://localhost:9325")
    monkeypatch.setattr(cli_helpers, "_server_version", lambda url: "1.9.0")
    monkeypatch.setattr("code_visualizer.utils.package_info.installed_version", lambda: "0.9.9")
    return out


def test_the_other_version_is_said_on_the_url_line(running, tmp_path):
    cli_helpers.graph_helper(watch_path=str(tmp_path), open_browser=False)
    line = [l for l in running.getvalue().splitlines() if "already runs" in l][0]
    # The desktop app reads it there (desktop/src-tauri: parse_server_line).
    assert line.endswith("http://localhost:9325/graph (cviz 1.9.0, not 0.9.9)")


def test_the_same_version_says_nothing_more(running, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_helpers, "_server_version", lambda url: "0.9.9")
    cli_helpers.graph_helper(watch_path=str(tmp_path), open_browser=False)
    assert "not 0.9.9" not in running.getvalue()


def test_restart_stops_the_other_version_then_serves(running, tmp_path, monkeypatch):
    stopped = []
    monkeypatch.setattr(cli_helpers, "_stop_running_server", lambda anchor: stopped.append(anchor) or True)

    class Started(Exception):
        pass

    def start(*a, **k):
        raise Started

    monkeypatch.setattr(cli_helpers, "_initialize_services", start)
    with pytest.raises(Started):
        cli_helpers.graph_helper(watch_path=str(tmp_path), open_browser=False, restart=True)
    assert stopped == [tmp_path.resolve()]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_the_running_server_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("CVIZ_SERVERS_FILE", str(tmp_path / "servers.json"))
    server = subprocess.Popen(["sleep", "30"])
    try:
        server_registry.register(tmp_path, "http://localhost:9325", pid=server.pid)
        assert cli_helpers._stop_running_server(tmp_path / "a.py", timeout=5) is True
        assert server.wait(timeout=5) != 0
    finally:
        server.kill()
        server.wait()
