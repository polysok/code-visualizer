"""Package or framework of external C# types, from .csproj files and usings only."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_visualizer.tools.indexing.dotnet_packages import (
    attribute_external_types,
    external_type_origins,
    load_projects,
)
from code_visualizer.tools.indexing.resolution.calls import build_function_call_groups
from code_visualizer.tools.languages.csharp import CSharpTreeSitterParser
from code_visualizer.utils.tree_sitter_manager import get_tree_sitter_manager

API_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <JsonVersion>13.0.3</JsonVersion>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="$(JsonVersion)" />
    <PackageReference Include="Acme.Billing.Sql" Version="2.1.0" />
    <PackageReference Include="Acme.Common" />
    <Using Include="Acme.Common" />
  </ItemGroup>
</Project>
"""
TESTS_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Moq" Version="4.20.70" />
    <PackageReference Include="xunit" Version="2.4.2" />
    <ProjectReference Include="..\\Api\\Api.csproj" />
  </ItemGroup>
</Project>
"""
PACKAGES_PROPS = """<Project>
  <ItemGroup>
    <PackageVersion Include="Acme.Common" Version="5.0.1" />
  </ItemGroup>
</Project>
"""
API_SOURCE = """using Microsoft.AspNetCore.Mvc;
using Newtonsoft.Json;
using Acme.Billing;
namespace Api.Controllers {
  public class InvoiceController : ControllerBase {
    private readonly IInvoiceStore _store;
    public IActionResult Get(Guid id) {
      var json = JsonConvert.SerializeObject(id);
      _store.Load(id);
      var other = Guid.NewGuid();
      return Ok(json);
    }
  }
}
"""
TEST_SOURCE = """using Moq;
using Xunit;
namespace Api.Tests {
  public class InvoiceTests {
    private readonly Mock<IInvoiceStore> _store;
    public void Loads() { _store.Verify(); Assert.True(true); }
  }
}
"""


def _parse(path: Path, repo: Path) -> dict:
    manager = get_tree_sitter_manager()
    wrapper = MagicMock()
    wrapper.language_name = "c_sharp"
    wrapper.language = manager.get_language_safe("c_sharp")
    wrapper.parser = manager.create_parser("c_sharp")
    data = CSharpTreeSitterParser(wrapper).parse(path)
    data["repo_path"] = str(repo)
    return data


@pytest.fixture(scope="module")
def solution(tmp_path_factory):
    repo = tmp_path_factory.mktemp("sln")
    (repo / "Directory.Packages.props").write_text(PACKAGES_PROPS)
    (repo / "Api").mkdir()
    (repo / "Api" / "Api.csproj").write_text(API_CSPROJ)
    (repo / "Api" / "InvoiceController.cs").write_text(API_SOURCE)
    (repo / "Api" / "Tools.cs").write_text("namespace Acme.Tools { }\n")
    (repo / "Tests").mkdir()
    (repo / "Tests" / "Tests.csproj").write_text(TESTS_CSPROJ)
    (repo / "Tests" / "InvoiceTests.cs").write_text(TEST_SOURCE)
    data = [_parse(p, repo) for p in sorted(repo.rglob("*.cs"))]
    groups = build_function_call_groups(data, {})
    return repo, data, groups


def test_load_projects_reads_packages_versions_and_usings(solution):
    repo, _, _ = solution
    projects = {p.path.name: p for p in load_projects(repo)}
    api = projects["Api.csproj"]
    assert api.sdk == "Microsoft.NET.Sdk.Web"
    assert api.packages["Newtonsoft.Json"] == "13.0.3"  # $(Property) expanded
    assert api.packages["Acme.Common"] == "5.0.1"  # central package version
    assert "Microsoft.AspNetCore.Http" in api.usings  # Web SDK implicit usings
    assert "Acme.Common" in api.usings  # <Using Include>
    assert projects["Tests.csproj"].project_refs == [(repo / "Api" / "Api.csproj").resolve()]


def test_attribution(solution):
    _, data, groups = solution
    origins = {o["name"]: o for o in external_type_origins(data, groups)}
    assert origins["JsonConvert"]["package"] == "Newtonsoft.Json"
    assert origins["JsonConvert"]["package_version"] == "13.0.3"
    assert origins["JsonConvert"]["evidence"] == "known-type"
    assert origins["ControllerBase"]["origin"] == "framework"
    assert origins["Guid"]["origin"] == "framework"
    assert origins["Mock"]["package"] == "Moq"
    assert origins["Assert"]["package"] == "xunit"
    # Visible namespaces: Acme.Billing (only Acme.Billing.Sql declared, so that
    # package), Acme.Common (<Using Include>), Newtonsoft.Json (well known,
    # listed types only) and the framework (unlisted type): two candidates left.
    assert origins["IInvoiceStore"]["origin"] == "ambiguous"
    assert origins["IInvoiceStore"]["candidates"] == ["Acme.Billing.Sql", "Acme.Common"]


def test_namespace_under_a_declared_package(tmp_path):
    fd = {
        "path": str(tmp_path / "A.cs"), "lang": "c_sharp", "package_name": "App",
        "imports": [{"name": "Acme.Billing"}, {"name": "Newtonsoft.Json"}],
    }
    (tmp_path / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Acme.Billing.Sql" Version="2.1.0" />'
        '<PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
        "</ItemGroup></Project>"
    )
    rows = [{"external_type": "IInvoiceStore", "caller_file_path": fd["path"], "full_call_name": "s.Load"}]
    [origin] = attribute_external_types([fd], rows, load_projects(tmp_path))
    assert (origin["package"], origin["package_version"], origin["evidence"]) == (
        "Acme.Billing.Sql", "2.1.0", "usings")


def test_unrelated_packages_stay_ambiguous(tmp_path):
    fd = {
        "path": str(tmp_path / "A.cs"), "lang": "c_sharp", "package_name": "App",
        "imports": [{"name": "Acme.One"}, {"name": "Acme.Two"}],
    }
    csproj = tmp_path / "App.csproj"
    csproj.write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Acme.One" Version="1" />'
        '<PackageReference Include="Acme.Two" Version="1" />'
        "</ItemGroup></Project>"
    )
    rows = [{"external_type": "Widget", "caller_file_path": fd["path"], "full_call_name": "w.Run"}]
    [origin] = attribute_external_types([fd], rows, load_projects(tmp_path))
    assert origin["origin"] == "ambiguous"
    assert origin["candidates"] == ["Acme.One", "Acme.Two"]


def test_origins_are_written_to_ladybug(solution, tmp_path):
    pytest.importorskip("ladybug")
    from code_visualizer.core.database_ladybug import LadybugDBManager
    from code_visualizer.tools.indexing.persistence.writer import GraphWriter

    repo, data, groups = solution
    manager = LadybugDBManager(str(tmp_path / "db"))
    try:
        writer = GraphWriter(manager.get_driver())
        writer.add_repository_to_graph(repo)
        for fd in data:
            writer.add_file_to_graph(fd, repo.name, {}, repo_path_str=str(repo.resolve()))
        writer.write_function_call_groups(*groups)
        writer.write_external_class_origins(external_type_origins(data, groups))
        with manager.get_driver().session() as session:
            rows = session.run(
                "MATCH (e:ExternalClass) RETURN e.name AS name, e.origin AS origin, "
                "e.package AS package, e.package_version AS version, e.candidates AS candidates"
            ).data()
    finally:
        manager.close_driver()
    by_name = {r["name"]: r for r in rows}
    assert by_name["JsonConvert"]["package"] == "Newtonsoft.Json"
    assert by_name["JsonConvert"]["version"] == "13.0.3"
    assert by_name["Guid"]["origin"] == "framework"
    assert by_name["Guid"]["candidates"] in ([], None)
