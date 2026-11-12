"""The graph page shows where external C# types come from and can leave out the
framework and the tests (on a real LadybugDB index of a small solution)."""
from unittest.mock import MagicMock

import pytest

from code_visualizer.tools.indexing.dotnet_packages import external_type_origins
from code_visualizer.tools.indexing.resolution.calls import build_function_call_groups
from code_visualizer.tools.languages.csharp import CSharpTreeSitterParser
from code_visualizer.utils.tree_sitter_manager import get_tree_sitter_manager
from code_visualizer.viz import neighborhood as nb

API_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>
  <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
</ItemGroup></Project>"""
TESTS_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>
  <PackageReference Include="Moq" Version="4.20.70" />
  <ProjectReference Include="..\\Api\\Api.csproj" />
</ItemGroup></Project>"""
SVC = """using System;
using Newtonsoft.Json;
namespace Api {
  public class Svc {
    public string Run(Guid id) {
      var a = JsonConvert.SerializeObject(id);
      var b = JsonConvert.DeserializeObject(a);
      return Guid.NewGuid().ToString();
    }
  }
}
"""
SVC_TESTS = """using Moq;
using Api;
namespace Api.Tests {
  public class SvcTests {
    private readonly Mock<IClock> _clock;
    public void Runs() { var svc = new Svc(); svc.Run(System.Guid.Empty); _clock.Verify(); }
  }
}
"""


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    pytest.importorskip("ladybug")
    from code_visualizer.core.database_ladybug import LadybugDBManager
    from code_visualizer.tools.indexing.persistence.writer import GraphWriter

    repo = tmp_path_factory.mktemp("sln")
    for rel, text in {
        "Api/Api.csproj": API_CSPROJ, "Api/Svc.cs": SVC,
        "Api.Tests/Api.Tests.csproj": TESTS_CSPROJ, "Api.Tests/SvcTests.cs": SVC_TESTS,
    }.items():
        (repo / rel).parent.mkdir(exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    manager = get_tree_sitter_manager()
    wrapper = MagicMock()
    wrapper.language_name = "c_sharp"
    wrapper.language = manager.get_language_safe("c_sharp")
    wrapper.parser = manager.create_parser("c_sharp")
    data = []
    for path in sorted(repo.rglob("*.cs")):
        fd = CSharpTreeSitterParser(wrapper).parse(path)
        fd["repo_path"] = str(repo)
        data.append(fd)
    groups = build_function_call_groups(data, {})

    db = LadybugDBManager(str(tmp_path_factory.mktemp("db") / "db"))
    writer = GraphWriter(db.get_driver())
    writer.add_repository_to_graph(repo)
    for fd in data:
        writer.add_file_to_graph(fd, repo.name, {}, repo_path_str=str(repo.resolve()))
    writer.write_function_call_groups(*groups)
    writer.write_external_class_origins(external_type_origins(data, groups))
    with db.get_driver().session() as s:
        paths = {r["name"]: r["path"] for r in s.run(
            "MATCH (f:Function) RETURN f.name AS name, f.path AS path").data()}
        yield s, paths
    db.close_driver()


def _externals(data):
    by_id = {n["id"]: n for n in data["nodes"]}
    out = {}
    for link in data["links"]:
        node = by_id[link["target"]]
        if node["kind"] == "ExternalClass":
            out[node["name"]] = (node, link.get("members"))
    return out


def test_external_nodes_carry_origin_and_called_members(session):
    s, paths = session
    ext = _externals(nb.neighborhood(s, path=paths["Run"], name="Run", line=5))
    node, members = ext["JsonConvert"]
    assert (node["origin"], node["package"], node["package_version"]) == ("package", "Newtonsoft.Json", "13.0.3")
    assert members == ["DeserializeObject", "SerializeObject"]
    assert ext["Guid"][0]["origin"] == "framework"


def test_framework_types_can_be_left_out(session):
    s, paths = session
    data = nb.neighborhood(s, path=paths["Run"], name="Run", line=5, hide_framework=True)
    assert "Guid" not in _externals(data) and "JsonConvert" in _externals(data)
    assert data["filtered"]["framework"] >= 1


def test_tests_can_be_left_out_except_around_a_test(session):
    s, paths = session
    callers = lambda d: {n["name"] for n in d["nodes"] if n["role"] == "caller"}  # noqa: E731
    assert "Runs" in callers(nb.neighborhood(s, path=paths["Run"], name="Run", line=5))
    hidden = nb.neighborhood(s, path=paths["Run"], name="Run", line=5, hide_tests=True)
    assert "Runs" not in callers(hidden) and hidden["filtered"]["tests"] == 1
    # Centered on the test itself, its Moq calls stay.
    around_test = nb.neighborhood(s, path=paths["Runs"], name="Runs", line=6, hide_tests=True)
    assert "Mock" in _externals(around_test)


@pytest.mark.parametrize(("path", "expected"), [
    ("/r/Api.Tests/SvcTests.cs", True),
    ("/r/src/Api.UnitTests/Foo.cs", True),
    ("/r/tests/unit/x.py", True),
    ("/r/web/app.spec.ts", True),
    ("/r/Api/Services/Svc.cs", False),
    ("/r/Api/Contest.cs", False),
    ("/r/Api/Testimonials/Page.cs", False),
])
def test_is_test_path(path, expected):
    assert nb.is_test_path(path) is expected
