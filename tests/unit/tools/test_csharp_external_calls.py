"""C# calls on package or DLL types reach the graph as CALLS -> ExternalClass.

A call whose target type is not in the indexed sources had no node to point
at: the resolver either dropped it (receiver type known but not indexed) or
guessed a target in the caller's file that did not exist, and the writer
dropped the edge silently. On an ASP.NET solution that was 87% of the calls.
"""
from unittest.mock import MagicMock

import pytest

from code_visualizer.tools.indexing.resolution.calls import build_function_call_groups
from code_visualizer.tools.indexing.resolution.csharp_external import external_receiver_type
from code_visualizer.tools.languages.csharp import CSharpTreeSitterParser
from code_visualizer.utils.tree_sitter_manager import get_tree_sitter_manager

SOURCE = """using System;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json;
namespace Demo {
  public class Repo { public void Save(int x) {} }
  public class Svc {
    private readonly ILogger<Svc> _logger;
    private readonly Repo _repo;
    public string Name { get; set; }
    public Svc(ILogger<Svc> logger, Repo repo) { _logger = logger; _repo = repo; }
    public void Run(string input) {
      _logger.LogInformation("start");
      var id = Guid.NewGuid();
      if (string.IsNullOrEmpty(input)) { return; }
      var text = JsonConvert.SerializeObject(id);
      var path = System.IO.Path.Combine("a", "b");
      _repo.Save(1);
      HttpContext.User.FindFirst("x");
      Name.Trim();
      unknown.Do();
    }
  }
}
"""


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    manager = get_tree_sitter_manager()
    wrapper = MagicMock()
    wrapper.language_name = "c_sharp"
    wrapper.language = manager.get_language_safe("c_sharp")
    wrapper.parser = manager.create_parser("c_sharp")
    repo = tmp_path_factory.mktemp("repo")
    path = repo / "Svc.cs"
    path.write_text(SOURCE, encoding="utf-8")
    data = CSharpTreeSitterParser(wrapper).parse(path)
    data["repo_path"] = str(repo)
    return repo, data


def _external_rows(data):
    groups = build_function_call_groups([data], {})
    return groups, {(r["full_call_name"], r["external_type"]) for r in groups[10]}


def test_calls_on_external_types_target_external_class(parsed):
    _, data = parsed
    _, external = _external_rows(data)
    assert ("_logger.LogInformation", "ILogger") in external
    assert ("Guid.NewGuid", "Guid") in external
    assert ("string.IsNullOrEmpty", "String") in external
    assert ("JsonConvert.SerializeObject", "JsonConvert") in external
    assert ("System.IO.Path.Combine", "Path") in external
    assert ("Name.Trim", "String") in external  # property typed `string`


def test_internal_member_chain_and_unknown_receivers_stay_out(parsed):
    _, data = parsed
    groups, external = _external_rows(data)
    names = {name for name, _ in external}
    assert "_repo.Save" not in names  # internal type: normal resolution
    assert "HttpContext.User.FindFirst" not in names  # member chain, not a namespace
    assert "unknown.Do" not in names  # type unknowable
    internal = [r for r in groups[0] if r["full_call_name"] == "_repo.Save"]
    assert internal and internal[0]["called_name"] == "Save"


def test_external_row_keeps_member_and_caller(parsed):
    _, data = parsed
    groups, _ = _external_rows(data)
    row = next(r for r in groups[10] if r["full_call_name"] == "Guid.NewGuid")
    assert row["called_name"] == "NewGuid"
    assert row["caller_name"] == "Run"
    assert row["resolution_tier"] == 5


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ({"name": "M", "full_name": "Foo.M", "inferred_obj_type": None}, "Foo"),
        ({"name": "M", "full_name": "Local.M", "inferred_obj_type": None}, None),
        ({"name": "M", "full_name": "x.M", "inferred_obj_type": "Local"}, None),
        ({"name": "M", "full_name": "x.M", "inferred_obj_type": "int"}, "Int32"),
        ({"name": "M", "full_name": "Acme.Lib.Foo.M", "inferred_obj_type": None}, "Foo"),
        ({"name": "M", "full_name": "Other.Thing.M", "inferred_obj_type": None}, None),
        ({"name": "nameof", "full_name": "nameof", "inferred_obj_type": None}, None),
        ({"name": "M", "full_name": "Prop.M", "inferred_obj_type": None}, None),
    ],
)
def test_external_receiver_type(call, expected):
    file_data = {"imports": [{"name": "Acme.Lib"}]}
    assert external_receiver_type(call, file_data, {"Local"}, {"Prop"}) == expected


def test_skip_external_resolution_keeps_them_out(parsed, monkeypatch):
    import code_visualizer.tools.indexing.resolution.calls as calls_mod

    _, data = parsed
    monkeypatch.setattr(
        calls_mod, "get_config_value",
        lambda key: "true" if key == "SKIP_EXTERNAL_RESOLUTION" else None,
    )
    assert build_function_call_groups([data], {})[10] == []


def test_external_calls_are_written_to_ladybug(parsed, tmp_path):
    pytest.importorskip("ladybug")
    from code_visualizer.core.database_ladybug import LadybugDBManager
    from code_visualizer.tools.indexing.persistence.writer import GraphWriter

    repo, data = parsed
    manager = LadybugDBManager(str(tmp_path / "db"))
    try:
        writer = GraphWriter(manager.get_driver())
        writer.add_repository_to_graph(repo)
        writer.add_file_to_graph(data, repo.name, {}, repo_path_str=str(repo.resolve()))
        writer.write_function_call_groups(*build_function_call_groups([data], {}))
        with manager.get_driver().session() as session:
            rows = session.run(
                "MATCH (f:Function)-[r:CALLS]->(e:ExternalClass) "
                "RETURN f.name AS caller, e.name AS type, r.called_name AS member"
            ).data()
    finally:
        manager.close_driver()
    found = {(r["caller"], r["type"], r["member"]) for r in rows}
    assert ("Run", "Guid", "NewGuid") in found
    assert ("Run", "ILogger", "LogInformation") in found
    assert ("Run", "String", "IsNullOrEmpty") in found

    # Reopening an existing database re-runs the CALLS -> ExternalClass migration.
    LadybugDBManager(str(tmp_path / "db")).close_driver()


CONTROLLER_SOURCE = """using System;
using Microsoft.AspNetCore.Mvc;
namespace Demo {
  public class Model { }
  public abstract class ApiBase : ControllerBase, IDisposable {
    public void Dispose() { }
  }
  public class UsersController : ApiBase {
    public IActionResult Get(string id) {
      if (id == null) { throw new ArgumentNullException(nameof(id)); }
      var claim = User.FindFirst("sub");
      Response.Headers.Append("x", "y");
      var model = new Model();
      var sb = new System.Text.StringBuilder();
      Helper();
      return Ok(model);
    }
    private void Helper() { }
  }
}
"""


@pytest.fixture(scope="module")
def controller_rows(tmp_path_factory):
    manager = get_tree_sitter_manager()
    wrapper = MagicMock()
    wrapper.language_name = "c_sharp"
    wrapper.language = manager.get_language_safe("c_sharp")
    wrapper.parser = manager.create_parser("c_sharp")
    path = tmp_path_factory.mktemp("ctl") / "UsersController.cs"
    path.write_text(CONTROLLER_SOURCE, encoding="utf-8")
    data = CSharpTreeSitterParser(wrapper).parse(path)
    groups = build_function_call_groups([data], {})
    return groups, {(r["called_name"], r["external_type"]) for r in groups[10]}


def test_bare_call_reaches_the_external_base_through_internal_classes(controller_rows):
    _, external = controller_rows
    assert ("Ok", "ControllerBase") in external  # UsersController -> ApiBase -> ControllerBase


def test_inherited_framework_property_gets_its_type(controller_rows):
    _, external = controller_rows
    assert ("FindFirst", "ClaimsPrincipal") in external
    assert not any(t in ("User", "Response") for _, t in external)


def test_constructors_of_external_types(controller_rows):
    _, external = controller_rows
    assert ("ArgumentNullException", "ArgumentNullException") in external
    assert ("System.Text.StringBuilder", "StringBuilder") in external
    assert not any(t == "Model" for _, t in external)  # internal type


def test_local_methods_and_nameof_are_not_external(controller_rows):
    groups, external = controller_rows
    assert not any(name in ("Helper", "nameof") for name, _ in external)
    assert any(r["called_name"] == "Helper" for r in groups[0])
