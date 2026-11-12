"""POST /api/update (cviz update / git hooks while `cviz graph` holds the database)."""
from fastapi.testclient import TestClient

from code_visualizer.cli import cli_helpers
from code_visualizer.viz import graph_api, server


def _client(monkeypatch, roots, watcher=None):
    calls = []

    def fake_update(builder, repo, cvizignore_path=None):
        calls.append(("oneshot", str(repo)))
        return {"changed": 2, "removed": 0, "mode": "incremental"}

    monkeypatch.setattr("code_visualizer.core.watcher.incremental_update", fake_update)
    monkeypatch.setattr(graph_api, "indexed_repo_roots", lambda max_age=30.0: roots)
    monkeypatch.setattr(graph_api, "_index_services",
                        {"graph_builder": object(), "watcher": watcher, "cvizignore_path": None})
    return TestClient(server.app), calls


def test_update_runs_on_the_repository_containing_the_path(monkeypatch, tmp_path):
    repo = tmp_path.resolve().as_posix()
    client, calls = _client(monkeypatch, [repo])
    body = client.post("/api/update", json={"path": f"{repo}/src/a.py"}).json()
    assert body == {"changed": 2, "removed": 0, "mode": "incremental", "repo": repo}
    assert calls == [("oneshot", repo)]


def test_update_goes_through_the_watcher_of_a_watched_repository(monkeypatch, tmp_path):
    repo = tmp_path.resolve().as_posix()

    class Handler:
        def catch_up_with_disk(self):
            return {"changed": 0, "removed": 0, "mode": "none"}

    class Watcher:
        handlers = {str(tmp_path): Handler()}

    client, calls = _client(monkeypatch, [repo], Watcher())
    assert client.post("/api/update", json={"path": repo}).json()["mode"] == "none"
    assert calls == []


def test_update_refuses_unknown_paths_remote_callers_and_servers_without_builder(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, [tmp_path.resolve().as_posix()])
    assert client.post("/api/update", json={"path": "/elsewhere"}).status_code == 404
    assert client.post("/api/update", json={"path": "/x"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    monkeypatch.setattr(graph_api, "_index_services", {"graph_builder": None, "watcher": None, "cvizignore_path": None})
    assert client.post("/api/update", json={"path": "/x"}).status_code == 503


def test_cli_update_falls_back_to_this_process_without_a_server(monkeypatch, tmp_path):
    monkeypatch.setenv("CVIZ_GRAPH_URL", "http://127.0.0.1:9")  # nothing listens there
    assert cli_helpers._update_through_server(str(tmp_path)) is None


def test_graph_config_names_the_served_repository(monkeypatch, tmp_path):
    class Watcher:
        handlers = {str(tmp_path): object()}

    monkeypatch.setattr(graph_api, "_index_services", {
        "graph_builder": object(), "watcher": Watcher(), "cvizignore_path": None,
        "repo_root": tmp_path.resolve().as_posix()})
    repo = TestClient(server.app).get("/api/graph-config").json()["repo"]
    assert repo == {"name": tmp_path.name, "path": tmp_path.resolve().as_posix(), "watched": True,
                    "companions": [], "companions_pending": False}
