"""cviz-open hands the artifact to the running desktop app (desktop.json)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from code_visualizer.cli import open_in_graph


@pytest.fixture
def desktop_app(tmp_path, monkeypatch):
    """A stand-in for the app's IDE endpoint (desktop/src-tauri, ide_link)."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            ok = self.path == "/open" and self.headers.get("X-CViz-Token") == "secret"
            if ok:
                received.append(json.loads(body))
            self.send_response(200 if ok else 403)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    record = tmp_path / "desktop.json"
    record.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "token": "secret", "pid": 1}))
    monkeypatch.setenv("CVIZ_DESKTOP_FILE", str(record))
    yield record, received
    server.shutdown()


def test_the_running_app_receives_the_artifact(desktop_app, tmp_path, monkeypatch):
    record, received = desktop_app
    source = tmp_path / "repo" / "a.py"
    source.parent.mkdir()
    (tmp_path / "repo" / ".git").mkdir()
    source.write_text("x = 1\n")
    opened = []
    monkeypatch.setattr(open_in_graph.webbrowser, "open", lambda *a, **k: opened.append(a))

    assert open_in_graph.main([f"{source}:3"]) == 0
    assert received == [{"path": str(source.resolve()), "line": 3, "repo": str((tmp_path / "repo").resolve())}]
    assert opened == []


def test_a_wrong_token_or_no_app_falls_back(desktop_app, tmp_path, monkeypatch):
    record, received = desktop_app
    data = json.loads(record.read_text())
    record.write_text(json.dumps({**data, "token": "wrong"}))
    assert open_in_graph.open_in_desktop(str(tmp_path / "a.py"), 1) is False
    record.write_text(json.dumps({"url": "http://127.0.0.1:1", "token": "secret"}))
    assert open_in_graph.open_in_desktop(str(tmp_path / "a.py"), 1) is False
    record.unlink()
    assert open_in_graph.open_in_desktop(str(tmp_path / "a.py"), 1) is False
    assert received == []


def test_only_a_loopback_url_is_trusted(desktop_app, tmp_path):
    record, received = desktop_app
    record.write_text(json.dumps({"url": "http://example.com:80", "token": "secret"}))
    assert open_in_graph.open_in_desktop(str(tmp_path / "a.py"), 1) is False


def test_browser_flag_skips_the_app(desktop_app, tmp_path, monkeypatch):
    record, received = desktop_app
    monkeypatch.setattr(open_in_graph, "resolve_server", lambda path, explicit=None: ("http://localhost:1", True))
    monkeypatch.setattr(open_in_graph, "focus_existing", lambda *a: 0)
    monkeypatch.setattr(open_in_graph.webbrowser, "open", lambda *a, **k: None)
    assert open_in_graph.main([str(tmp_path / "a.py"), "--browser"]) == 0
    assert received == []


def test_an_installed_app_that_is_not_running_is_started(desktop_app, monkeypatch, tmp_path):
    record, received = desktop_app
    data = record.read_text()
    record.unlink()  # not running: no endpoint recorded
    launched = []

    def launch():
        launched.append(1)
        record.write_text(data)  # the app records its endpoint once up
        return True

    monkeypatch.setattr(open_in_graph, "launch_desktop", launch)
    assert open_in_graph.show_in_desktop(str(tmp_path / "a.py"), 2, str(tmp_path), timeout=5) is True
    assert launched == [1]
    assert received == [{"path": str(tmp_path / "a.py"), "line": 2, "repo": str(tmp_path)}]


def test_no_app_installed_means_no_desktop(desktop_app, monkeypatch, tmp_path):
    record, received = desktop_app
    record.unlink()
    monkeypatch.setattr(open_in_graph, "launch_desktop", lambda: False)
    assert open_in_graph.show_in_desktop(str(tmp_path / "a.py"), 1) is False


def test_tests_never_start_the_real_app():
    assert open_in_graph.launch_desktop() is False  # CVIZ_DESKTOP_LAUNCH=0 (conftest)


class TestCvizGraphShowsThePage:
    """cviz graph: the desktop app when installed, else the browser."""

    @pytest.fixture
    def browser(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url))
        return opened

    def test_the_repository_goes_to_the_app(self, desktop_app, browser, tmp_path):
        from code_visualizer.cli.cli_helpers import _show_graph

        record, received = desktop_app
        _show_graph("http://localhost:1", "http://localhost:1/graph", str(tmp_path), None, None, False)
        assert received == [{"path": str(tmp_path), "line": None, "repo": str(tmp_path)}]
        assert browser == []

    def test_an_artifact_goes_to_the_app_centred(self, desktop_app, browser, tmp_path):
        from code_visualizer.cli.cli_helpers import _show_graph

        record, received = desktop_app
        source = tmp_path / "a.py"
        source.write_text("x = 1\n")
        _show_graph("http://localhost:1", "u", str(tmp_path), f"{source}:4", None, False)
        assert received == [{"path": str(source.resolve()), "line": 4, "repo": str(tmp_path)}]

    @pytest.mark.parametrize("changes, browser_only, env", [
        ("origin/main", False, None),  # the review has no tab in the app
        (None, True, None),            # --browser
        (None, False, "browser"),      # CVIZ_OPEN_IN=browser
    ])
    def test_the_browser_when_asked_or_needed(self, desktop_app, browser, tmp_path, monkeypatch,
                                              changes, browser_only, env):
        from code_visualizer.cli.cli_helpers import _show_graph

        if env:
            monkeypatch.setenv("CVIZ_OPEN_IN", env)
        record, received = desktop_app
        _show_graph("http://localhost:1", "http://x/graph", str(tmp_path), None, changes, browser_only)
        assert received == [] and browser == ["http://x/graph"]

    def test_no_app_falls_back_to_the_browser(self, desktop_app, browser, tmp_path):
        from code_visualizer.cli.cli_helpers import _show_graph

        record, received = desktop_app
        record.unlink()
        _show_graph("http://localhost:1", "http://x/graph", str(tmp_path), None, None, False)
        assert browser == ["http://x/graph"]
