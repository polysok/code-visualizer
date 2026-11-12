"""A repository indexed with its companion sources: its calls into their code
become real links instead of ExternalClass nodes, also after an incremental
update of one of its files (real LadybugDB)."""
import asyncio
from pathlib import Path

import pytest

from code_visualizer.core import companions
from code_visualizer.core.database_ladybug import LadybugDBManager
from code_visualizer.core.jobs import JobManager
from code_visualizer.tools.graph_builder import GraphBuilder

pytest.importorskip("ladybug")

APP = """using Acme.Billing;
namespace App {
  public class Svc {
    private readonly IInvoiceStore _store;
    public decimal Run() {
      _store.Load(1);
      var calc = new InvoiceCalc();
      return calc.Total(2);
    }
  }
}
"""
STORE = """namespace Acme.Billing {
  public interface IInvoiceStore { void Load(int id); }
}
"""
CALC = """namespace Acme.Billing {
  public class InvoiceCalc { public decimal Total(int n) { return n; } }
}
"""


class _DBM:
    def __init__(self, driver):
        self._driver = driver

    def get_driver(self, graph_name=None):
        return self._driver

    def get_backend_type(self):
        return "ladybugdb"


@pytest.fixture
def repos(tmp_path):
    main, lib = tmp_path / "App", tmp_path / "Acme.Billing"
    main.mkdir()
    lib.mkdir()
    (main / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Acme.Billing" Version="2.0.0" /></ItemGroup></Project>')
    (main / "Svc.cs").write_text(APP)
    (lib / "Acme.Billing.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk" />')
    (lib / "IInvoiceStore.cs").write_text(STORE)
    (lib / "InvoiceCalc.cs").write_text(CALC)
    return main, lib


def _index(tmp_path, main):
    manager = LadybugDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    gb = GraphBuilder(_DBM(driver), JobManager(), asyncio.new_event_loop())
    asyncio.new_event_loop().run_until_complete(gb.build_graph_from_path_async(main, is_dependency=False))
    return manager, gb


def _calls(driver):
    with driver.session() as s:
        rows = s.run(
            "MATCH (f:Function {name: 'Run'})-[r:CALLS]->(t) "
            "RETURN labels(t)[0] AS label, t.name AS name, t.path AS path, r.called_name AS member"
        ).data()
    return {(r["label"], r["name"], Path(r["path"]).name if r["path"] else None) for r in rows}


def test_without_companions_the_calls_end_on_external_classes(tmp_path, repos):
    main, _ = repos
    manager, _ = _index(tmp_path, main)
    try:
        calls = _calls(manager.get_driver())
    finally:
        manager.close_driver()
    assert ("ExternalClass", "IInvoiceStore", None) in calls
    assert ("ExternalClass", "InvoiceCalc", None) in calls


def test_companion_code_becomes_real_links(tmp_path, repos):
    main, lib = repos
    companions.add(main, "../Acme.Billing")
    manager, gb = _index(tmp_path, main)
    try:
        driver = manager.get_driver()
        calls = _calls(driver)
        with driver.session() as s:
            repos_rows = s.run("MATCH (r:Repository) RETURN r.name AS name, r.is_dependency AS dep").data()
    finally:
        manager.close_driver()
    assert ("Function", "Total", "InvoiceCalc.cs") in calls
    assert not any(label == "ExternalClass" and name in ("IInvoiceStore", "InvoiceCalc")
                   for label, name, _ in calls)
    assert {(r["name"], r["dep"]) for r in repos_rows} == {("App", False), ("Acme.Billing", True)}
    assert companions.indexed_roots(main) == [lib.resolve().as_posix()]
    assert companions.describe(main)["pending"] is False


def test_incremental_update_keeps_links_into_companions(tmp_path, repos):
    from code_visualizer.core.watcher import RepositoryEventHandler

    main, _ = repos
    companions.add(main, "../Acme.Billing")
    manager, gb = _index(tmp_path, main)
    try:
        svc = main / "Svc.cs"
        svc.write_text(APP.replace("calc.Total(2)", "calc.Total(3) + 1"))
        handler = RepositoryEventHandler(gb, main, perform_initial_scan=False)
        handler.apply_changes([str(svc)], [])
        calls = _calls(manager.get_driver())
    finally:
        manager.close_driver()
    assert ("Function", "Total", "InvoiceCalc.cs") in calls
    assert not any(label == "ExternalClass" and name == "InvoiceCalc" for label, name, _ in calls)
