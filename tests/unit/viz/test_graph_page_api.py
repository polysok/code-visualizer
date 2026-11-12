"""Unit tests for the dependency-graph page API (viz/graph_api.py, viz/neighborhood.py)."""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from code_visualizer.viz import graph_api, neighborhood as nb, server


# ---------------------------------------------------------------------------
# PathMapper
# ---------------------------------------------------------------------------


def test_path_mapper_round_trip_and_longest_prefix():
    m = nb.PathMapper.parse("/srv/build=/Users/me/code; /srv/build/lib=/opt/lib\n")
    assert m.to_local("/srv/build/app/a.py") == "/Users/me/code/app/a.py"
    assert m.to_local("/srv/build/lib/x.py") == "/opt/lib/x.py"  # nested rule wins
    assert m.to_local("/elsewhere/a.py") == "/elsewhere/a.py"
    assert m.to_indexed("/Users/me/code/app/a.py") == "/srv/build/app/a.py"
    # A sibling directory sharing the prefix text is not rewritten.
    assert m.to_local("/srv/builder/a.py") == "/srv/builder/a.py"


def test_path_mapper_windows_separators():
    m = nb.PathMapper.parse(r"C:\build\repo=/home/me/repo")
    assert m.to_local(r"C:\build\repo\src\a.cs") == "/home/me/repo/src/a.cs"


def test_node_id_distinguishes_homonyms():
    a = nb.node_id("Function", "/r/a.py", "run", 3)
    b = nb.node_id("Function", "/r/b.py", "run", 3)
    c = nb.node_id("Function", "/r/a.py", "run", 10)
    assert len({a, b, c}) == 3
    assert nb.node_id("File", "/r/a.py", "a.py", 1) == "File|/r/a.py"


# ---------------------------------------------------------------------------
# A tiny in-memory "session" answering the queries neighborhood.py issues
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return list(self._rows)


class FakeGraph:
    """Minimal graph: artifacts + typed edges, matched by the query's shape."""

    def __init__(self):
        self.nodes: List[Dict[str, Any]] = []
        self.edges: List[Dict[str, Any]] = []
        self.files: List[str] = []

    def add(self, kind, path, name, line, end_line, cc=1):
        n = {"kind": kind, "path": path, "name": name, "line": line, "end_line": end_line, "cc": cc}
        self.nodes.append(n)
        return n

    def edge(self, rel, a, b, line=None):
        self.edges.append({"rel": rel, "a": a, "b": b, "line": line})

    def session(self):
        graph = self

        class _S:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def run(self, query, **p):
                return _Result(graph._answer(query, p))

        return _S()

    def _answer(self, q: str, p: Dict[str, Any]):
        if q.startswith("MATCH (f:File) WHERE f.path = $path RETURN"):
            return [{"path": p["path"], "name": p["path"].rsplit("/", 1)[-1]}] if p["path"] in self.files else []
        if "n.line_number <= $line" in q:
            label = q.split("(n:", 1)[1].split(")", 1)[0]
            return [
                {"name": n["name"], "path": n["path"], "line": n["line"], "end_line": n["end_line"], "complexity": n["cc"]}
                for n in self.nodes
                if n["kind"] == label and n["path"] == p["path"] and n["line"] <= p["line"] <= n["end_line"]
            ]
        if "WHERE n.name = $name" in q:
            label = q.split("(n:", 1)[1].split(")", 1)[0]
            return [
                {"name": n["name"], "path": n["path"], "line": n["line"], "end_line": n["end_line"]}
                for n in self.nodes
                if n["kind"] == label and n["name"] == p["name"] and ("path" not in p or n["path"] == p["path"])
            ]
        if "n.cyclomatic_complexity AS complexity" in q and "IN $paths" in q:
            return [
                {"path": n["path"], "name": n["name"], "line": n["line"], "complexity": n["cc"]}
                for n in self.nodes if n["kind"] == "Function"
            ]
        if "-[r:" in q:
            rels = q.split("-[r:", 1)[1].split("]", 1)[0].split("|")
            outgoing = "(a)-[r:" in q
            rows = []
            for e in self.edges:
                if e["rel"] not in rels:
                    continue
                a, b = (e["a"], e["b"]) if outgoing else (e["b"], e["a"])
                if a["path"] in p["paths"] and a["name"] in p.get("names", [a["name"]]):
                    rows.append({
                        "a_kind": a["kind"], "a_path": a["path"], "a_name": a["name"], "a_line": a["line"],
                        "b_kind": b["kind"], "b_path": b["path"], "b_name": b["name"], "b_line": b["line"],
                        "b_end_line": b["end_line"], "rel": e["rel"], "call_line": e["line"],
                    })
            return rows
        return []


@pytest.fixture
def graph():
    g = FakeGraph()
    g.files = ["/r/a.py", "/r/b.py"]
    run_a = g.add("Function", "/r/a.py", "run", 3, 6)
    run_b = g.add("Function", "/r/b.py", "run", 3, 6)
    cls = g.add("Class", "/r/a.py", "Job", 10, 30)
    method = g.add("Function", "/r/a.py", "execute", 12, 20, cc=4)
    base = g.add("Class", "/r/b.py", "BaseJob", 1, 2)
    helper_b = g.add("Function", "/r/b.py", "helper", 8, 9)
    g.add("Function", "/r/a.py", "<module>", 1, 40)
    g.edge("CALLS", method, run_a, line=15)
    g.edge("HEURISTIC_CALLS", method, helper_b, line=16)
    g.edge("CALLS", run_b, method, line=5)
    g.edge("INHERITS", cls, base)
    for i in range(10):
        caller = g.add("Function", f"/r/c{i}.py", "use", 1, 3)
        g.edge("CALLS", caller, run_a, line=2)
    return g


def test_locate_prefers_narrowest_and_function_over_class(graph):
    with graph.session() as s:
        assert nb.locate(s, "/r/a.py", 15)["name"] == "execute"
        assert nb.locate(s, "/r/a.py", 25)["name"] == "Job"
        assert nb.locate(s, "/r/a.py", 35)["name"] == "<module>"  # only the module spans it
        assert nb.locate(s, "/r/b.py", 50)["kind"] == "File"  # outside every artifact
        with pytest.raises(nb.ArtifactNotFound):
            nb.locate(s, "/not/indexed.py", 1)


def test_neighborhood_separates_homonyms_and_reports_call_lines(graph):
    with graph.session() as s:
        data = nb.neighborhood(s, path="/r/a.py", line=15)
    ids = {n["id"]: n for n in data["nodes"]}
    assert data["center"]["name"] == "execute"
    assert ids[nb.node_id("Function", "/r/a.py", "run", 3)]["role"] == "callee"
    assert ids[nb.node_id("Function", "/r/b.py", "run", 3)]["role"] == "caller"
    link = next(l for l in data["links"] if l["target"] == nb.node_id("Function", "/r/a.py", "run", 3))
    assert link["line"] == 15 and link["heuristic"] is False
    heuristic = next(l for l in data["links"] if l["target"] == nb.node_id("Function", "/r/b.py", "helper", 8))
    assert heuristic["heuristic"] is True
    assert data["center"]["complexity"] == 4


def test_neighborhood_caps_neighbours_and_counts_hidden(graph):
    with graph.session() as s:
        data = nb.neighborhood(s, path="/r/a.py", name="run", line=3, per_node_limit=4)
    center = data["center"]
    assert sum(1 for n in data["nodes"] if n["role"] == "caller") == 4
    # 10 "use" callers + execute = 11 callers, 4 shown.
    assert center["hidden"]["caller"] == 7
    assert data["truncated"] is True


def test_neighborhood_inheritance_and_depth(graph):
    with graph.session() as s:
        data = nb.neighborhood(s, path="/r/a.py", line=25, kinds=["INHERITS"])
    roles = {n["name"]: n["role"] for n in data["nodes"]}
    assert roles == {"Job": "center", "BaseJob": "parent"}
    with graph.session() as s:
        deep = nb.neighborhood(s, path="/r/b.py", name="run", line=3, depth=2, kinds=["CALLS"])
    names = {(n["name"], n["role"], n["depth"]) for n in deep["nodes"]}
    # run(b) -> execute -> run(a): callees keep expanding downstream at depth 2.
    assert ("execute", "callee", 1) in names
    assert ("run", "callee", 2) in names


def test_neighborhood_maps_paths(graph):
    mapper = nb.PathMapper.parse("/r=/home/me/r")
    with graph.session() as s:
        data = nb.neighborhood(s, path="/home/me/r/a.py", line=15, mapper=mapper)
    assert data["center"]["path"] == "/home/me/r/a.py"
    assert data["center"]["indexed_path"] == "/r/a.py"
    assert all(n["path"].startswith("/home/me/r/") for n in data["nodes"] if n["path"])


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class _Manager:
    def __init__(self, g):
        self.g = g

    def get_backend_type(self):
        return "ladybugdb"

    def get_driver(self):
        g = self.g

        class _D:
            def session(self, **_kw):
                return g.session()

        return _D()


@pytest.fixture
def client(graph, monkeypatch):
    monkeypatch.setattr(server, "db_manager", _Manager(graph))
    monkeypatch.setattr(graph_api, "indexed_repo_roots", lambda max_age=30.0: ["/r"])
    return TestClient(server.app)


def test_graph_page_and_assets_are_served(client):
    page = client.get("/graph")
    assert page.status_code == 200 and "graph.js" in page.text
    assert client.get("/graph-assets/graph.js").status_code == 200
    assert client.get("/graph-assets/d3.v7.min.js").status_code == 200
    assert client.get("/graph-assets/server.py").status_code == 404


def test_locate_and_neighborhood_endpoints(client):
    r = client.get("/api/locate", params={"path": "/r/a.py", "line": 15})
    assert r.status_code == 200 and r.json()["name"] == "execute"
    r = client.get("/api/locate", params={"path": "/nope.py", "line": 1})
    assert r.status_code == 404 and "not in the graph" in r.json()["detail"]
    r = client.get("/api/neighborhood", params={"path": "/r/a.py", "line": 15, "kinds": "CALLS"})
    body = r.json()
    assert r.status_code == 200 and body["center"]["name"] == "execute" and "elapsed_ms" in body
    assert client.get("/api/neighborhood", params={"path": "/r/a.py", "depth": 9}).status_code == 422


def test_resolve_lists_homonyms(client):
    r = client.get("/api/resolve", params={"symbol": "run"})
    assert {m["path"] for m in r.json()["matches"]} == {"/r/a.py", "/r/b.py"}


def test_remote_clients_need_api_key(client, monkeypatch):
    monkeypatch.setattr(graph_api, "_is_loopback", lambda request: False)
    monkeypatch.delenv("CVIZ_API_KEY", raising=False)
    monkeypatch.setattr("code_visualizer.api.auth.get_configured_api_key", lambda: None)
    assert client.get("/api/locate", params={"path": "/r/a.py", "line": 1}).status_code == 403
    monkeypatch.setattr("code_visualizer.api.auth.get_configured_api_key", lambda: "s3cret")
    assert client.get("/api/locate", params={"path": "/r/a.py", "line": 1}).status_code == 401
    ok = client.get("/api/locate", params={"path": "/r/a.py", "line": 15}, headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200


def test_focus_requires_json_and_local_origin(client):
    assert client.post("/api/focus", content=json.dumps({"path": "/r/a.py"})).status_code == 415
    evil = client.post("/api/focus", json={"path": "/r/a.py"}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    ok = client.post("/api/focus", json={"path": "/r/a.py", "line": 3}, headers={"Origin": "http://localhost:8000"})
    assert ok.status_code == 200 and ok.json() == {"delivered": 0}


def test_open_refuses_paths_outside_indexed_repos(client, tmp_path, monkeypatch):
    outside = tmp_path / "x.py"
    outside.write_text("pass\n")
    monkeypatch.setattr(graph_api, "is_path_allowed", lambda p: False)
    r = client.post("/api/open", json={"path": str(outside), "line": 1, "ide": "vscode"})
    assert r.status_code == 403
    r = client.post("/api/open", json={"path": "/r/a.py", "line": 1, "ide": "notepad"})
    assert r.status_code == 400


def test_open_launches_fixed_argv_without_shell(client, tmp_path, monkeypatch):
    target = tmp_path / "repo" / "a.py"
    target.parent.mkdir()
    target.write_text("pass\n")
    monkeypatch.setattr(graph_api, "indexed_repo_roots", lambda max_age=30.0: [str(target.parent.resolve())])
    monkeypatch.setattr(graph_api.shutil, "which", lambda exe: "/usr/bin/" + exe)
    launched = {}

    class _P:
        def __init__(self, argv, **kw):
            launched["argv"], launched["kw"] = argv, kw

    monkeypatch.setattr(graph_api.subprocess, "Popen", _P)
    r = client.post("/api/open", json={"path": str(target), "line": 7, "ide": "rider"})
    assert r.status_code == 200, r.text
    assert launched["argv"] == ["rider", "--line", "7", str(target)]
    assert "shell" not in launched["kw"]


def test_the_root_redirects_to_the_graph_page(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/graph"
    assert client.get("/favicon.ico").status_code == 404
