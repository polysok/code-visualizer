"""The assistant's graph tools (agent_tools) on a real LadybugDB index of a
small C# solution with a companion source."""
import asyncio

import pytest

from code_visualizer import agent_tools as at
from code_visualizer.core import companions
from code_visualizer.core.database_ladybug import LadybugDBManager
from code_visualizer.core.jobs import JobManager
from code_visualizer.tools.graph_builder import GraphBuilder

pytest.importorskip("ladybug")

SVC = """using Acme.Billing;
using Microsoft.Extensions.Logging;
namespace App.Services {
  public class Svc {
    private readonly ILogger<Svc> _logger;
    private readonly IInvoiceStore _store;
    public decimal Run(int id) {
      _logger.LogInformation("run");
      _store.Load(id);
      var calc = new InvoiceCalc();
      return calc.Total(id);
    }
    public decimal Run(int id, int times) { return Run(id) * times; }
  }
}
"""
CTRL = """namespace App.Controllers {
  public class InvoiceController {
    private readonly App.Services.Svc _svc;
    public decimal Get(int id) { return _svc.Run(id); }
  }
}
"""
CALC = """namespace Acme.Billing {
  public class InvoiceCalc {
    public decimal Total(int n) { return Round(n); }
    private decimal Round(int n) { return n; }
  }
}
"""


class _DBM:
    def __init__(self, manager):
        self.m = manager

    def get_driver(self, graph_name=None):
        return self.m.get_driver()

    def get_backend_type(self):
        return "ladybugdb"


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    root = tmp_path_factory.mktemp("sln")
    main, lib = root / "App", root / "Acme.Billing"
    (main / "Services").mkdir(parents=True)
    (main / "Controllers").mkdir()
    lib.mkdir()
    (main / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Microsoft.Extensions.Logging" Version="8.0.0" /></ItemGroup></Project>')
    (main / "Services" / "Svc.cs").write_text(SVC)
    (main / "Controllers" / "InvoiceController.cs").write_text(CTRL)
    (lib / "InvoiceCalc.cs").write_text(CALC)
    companions.add(main, "../Acme.Billing")
    manager = LadybugDBManager(str(root / "db"))
    gb = GraphBuilder(_DBM(manager), JobManager(), asyncio.new_event_loop())
    asyncio.new_event_loop().run_until_complete(gb.build_graph_from_path_async(main, is_dependency=False))
    yield manager
    manager.close_driver()


def _run(db, name, **args):
    result = at.call_tool(_DBM(db), name, args)
    assert "error" not in result, result
    return result["text"]


def test_overview_lists_the_repository_its_companion_and_entry_points(db):
    text = _run(db, "graph_overview")
    assert "App/  (main, " in text and "Acme.Billing/  (companion source, 1 files)" in text
    assert "Controllers/InvoiceController.cs:InvoiceController.Get@4" in text  # nothing calls it


def test_search_returns_ids_and_excerpts(db):
    text = _run(db, "search_code", query="Total", detail="signature")
    assert "Acme.Billing/InvoiceCalc.cs:InvoiceCalc.Total@3  [Function]" in text
    assert "public decimal Total(int n)" in text


def test_search_falls_back_to_names_without_a_search_index(db, monkeypatch):
    from code_visualizer.semantic import search

    monkeypatch.setattr(search.SearchEngine, "search", lambda self, q, limit=5: {"hits": [], "mode": "none"})
    text = _run(db, "search_code", query="invoice calc total")
    assert "matched on names only" in text
    assert "Acme.Billing/InvoiceCalc.cs:InvoiceCalc.Total@3" in text


def test_downstream_tree_crosses_into_the_companion_and_names_external_types(db):
    text = _run(db, "traverse_graph", start=["Services/Svc.cs:Svc.Run@7"], depth=2)
    lines = text.splitlines()
    assert lines[0] == "Services/Svc.cs:Svc.Run@7  [Function]"
    assert any("calls → Acme.Billing/InvoiceCalc.cs:InvoiceCalc.Total@3" in line for line in lines)
    # depth 2: Total -> Round, one level deeper in the tree
    assert any(line.startswith("│   └── calls → Acme.Billing/InvoiceCalc.cs:InvoiceCalc.Round@4")
               or line.startswith("    └── calls → Acme.Billing/InvoiceCalc.cs:InvoiceCalc.Round@4")
               for line in lines)
    assert any("external:IInvoiceStore" in line and ".Load" in line for line in lines)
    assert not any("external:ILogger" in line for line in lines)  # framework, left out by default
    assert "framework type(s) left out" in text

    text = _run(db, "traverse_graph", start=["Services/Svc.cs:Svc.Run@7"], include_framework=True)
    assert "external:ILogger" in text and ".LogInformation" in text


def test_upstream_finds_the_callers_and_overloads_are_told_apart(db):
    text = _run(db, "traverse_graph", start=["Services/Svc.cs:Svc.Run@7"], direction="upstream")
    assert "called by ← Controllers/InvoiceController.cs:InvoiceController.Get@4" in text
    assert "called by ← Services/Svc.cs:Svc.Run@13" in text  # the other overload
    assert "calls →" not in text


def test_an_external_type_lists_who_calls_it_and_what(db):
    text = _run(db, "traverse_graph", start=["external:IInvoiceStore"])
    assert "called by ← Services/Svc.cs:Svc.Run@7  [Function]  (.Load)" in text


def test_read_code_gives_numbered_source_a_file_outline_and_external_info(db):
    text = _run(db, "read_code", ids=["Acme.Billing/InvoiceCalc.cs:InvoiceCalc.Total",
                                      "Services/Svc.cs", "external:IInvoiceStore"])
    assert "    3      public decimal Total(int n) { return Round(n); }" in text
    assert "Services/Svc.cs  [File, 3 entities]" in text
    assert "Services/Svc.cs:Svc.Run@13  [Function]" in text
    assert "Members the code calls: Load" in text


def test_ids_survive_edits_and_bare_names_resolve_when_unique(db):
    # Stale line (the function moved): the nearest definition with that name wins.
    text = _run(db, "read_code", ids=["Acme.Billing/InvoiceCalc.cs:InvoiceCalc.Round@40"])
    assert "private decimal Round(int n)" in text
    assert "InvoiceController.Get@4" in _run(db, "traverse_graph", start=["InvoiceController.Get"])


def test_ambiguous_and_unknown_names_say_what_to_do(db):
    result = at.call_tool(_DBM(db), "traverse_graph", {"start": ["Run"]})
    assert "2 entities are named Run" in result["error"]
    assert "Services/Svc.cs:Svc.Run@7" in result["error"]
    result = at.call_tool(_DBM(db), "read_code", {"ids": ["Services/Svc.cs:Svc.Nope"]})
    assert "No Svc.Nope in Services/Svc.cs" in result["text"]


def test_graph_server_answers_the_relayed_calls(db, monkeypatch):
    """What the MCP server forwards when cviz graph holds the database."""
    from fastapi.testclient import TestClient

    from code_visualizer.viz import server

    monkeypatch.setattr(server, "db_manager", db)
    client = TestClient(server.app)
    r = client.post("/api/agent/read_code", json={"ids": ["Services/Svc.cs"]})
    assert r.status_code == 200 and "Services/Svc.cs  [File" in r.json()["text"]
    assert client.post("/api/agent/watch_directory", json={}).status_code == 404
    foreign = client.post("/api/agent/read_code", json={"ids": []},
                          headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403
