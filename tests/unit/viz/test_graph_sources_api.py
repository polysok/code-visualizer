"""The page lists, adds and removes companion sources and re-indexes with them
(/api/sources*, real LadybugDB)."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from code_visualizer.core.database_ladybug import LadybugDBManager
from code_visualizer.core.jobs import JobManager
from code_visualizer.tools.graph_builder import GraphBuilder
from code_visualizer.viz import graph_api, server

pytest.importorskip("ladybug")

APP = """namespace App {
  public class Svc { public decimal Run() { var c = new Acme.Billing.InvoiceCalc(); return c.Total(2); } }
}
"""
CALC = """namespace Acme.Billing {
  public class InvoiceCalc { public decimal Total(int n) { return n; } }
}
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    main, lib = tmp_path / "App", tmp_path / "Acme.Billing"
    main.mkdir()
    (lib / ".git").mkdir(parents=True)
    (main / "Svc.cs").write_text(APP)
    (lib / "InvoiceCalc.cs").write_text(CALC)
    (lib / "Acme.Billing.csproj").write_text("<Project/>")
    (main / "App.csproj").write_text(
        '<Project><ItemGroup><PackageReference Include="Acme.Billing" Version="1" /></ItemGroup></Project>')

    manager = LadybugDBManager(str(tmp_path / "db"))

    class _DBM:
        def get_driver(self, graph_name=None):
            return manager.get_driver()

        def get_backend_type(self):
            return "ladybugdb"

    gb = GraphBuilder(_DBM(), JobManager(), asyncio.new_event_loop())
    asyncio.new_event_loop().run_until_complete(gb.build_graph_from_path_async(main, is_dependency=False))
    monkeypatch.setattr(server, "db_manager", manager)
    monkeypatch.setitem(graph_api._index_services, "graph_builder", gb)
    monkeypatch.setitem(graph_api._index_services, "repo_root", main.resolve().as_posix())
    monkeypatch.setitem(graph_api._index_services, "watcher", None)
    monkeypatch.setattr(graph_api, "_reindex", {"thread": None, "job_id": None, "error": None, "finished_at": None})
    graph_api._repo_cache["ts"] = 0.0
    yield TestClient(server.app), main, lib
    manager.close_driver()


def _wait(c):
    graph_api._reindex["thread"].join(timeout=60)
    return c.get("/api/sources").json()


def _callees(c, main):
    body = c.get("/api/neighborhood", params={"path": str(main / "Svc.cs"), "name": "Run", "line": 2}).json()
    return {(n["kind"], n["name"]) for n in body["nodes"] if n["role"] == "callee"}


def test_add_reindex_remove(client):
    c, main, lib = client
    body = c.get("/api/sources").json()
    assert body["main"]["name"] == "App" and body["sources"] == [] and body["pending"] is False
    assert c.get("/api/graph-config").json()["repo"]["companions"] == []
    assert ("ExternalClass", "InvoiceCalc") in _callees(c, main)

    suggestions = c.get("/api/sources/suggestions").json()["suggestions"]
    assert suggestions[0]["name"] == "Acme.Billing" and suggestions[0]["provides"] == ["Acme.Billing"]

    headers = {"Content-Type": "application/json"}
    body = c.post("/api/sources", json={"action": "add", "path": "../Acme.Billing"}, headers=headers).json()
    [source] = body["sources"]
    assert (source["name"], source["kind"], source["indexed"], body["pending"]) == (
        "Acme.Billing", "repository", False, True)
    assert source["display_path"] == "../Acme.Billing"

    refused = c.post("/api/sources", json={"action": "add", "path": "."}, headers=headers)
    assert refused.status_code == 400 and "this repository" in refused.json()["detail"]

    assert c.post("/api/sources/reindex", json={}, headers=headers).status_code == 202
    body = _wait(c)
    assert body["reindex"]["error"] is None and body["pending"] is False
    assert body["sources"][0]["indexed"] is True and body["sources"][0]["files"] == 1
    assert ("Function", "Total") in _callees(c, main)
    assert [x["name"] for x in c.get("/api/graph-config").json()["repo"]["companions"]] == ["Acme.Billing"]

    body = c.post("/api/sources", json={"action": "remove", "path": str(lib)}, headers=headers).json()
    assert body["sources"] == [] and body["removed_pending"] == [lib.resolve().as_posix()] and body["pending"]
    c.post("/api/sources/reindex", json={}, headers=headers)
    body = _wait(c)
    assert body["pending"] is False and body["removed_pending"] == []
    assert ("ExternalClass", "InvoiceCalc") in _callees(c, main)


def test_other_sites_cannot_change_the_sources(client):
    c, main, _ = client
    r = c.post("/api/sources", json={"action": "add", "path": "../Acme.Billing"},
               headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.post("/api/sources/reindex", json={},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert not (main / ".code-visualizer" / "companions.json").exists()
