"""The Changes view: entities a branch adds or modifies against its target
branch, and who is impacted (real git repository, real LadybugDB index)."""
import asyncio
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from code_visualizer.core.database_ladybug import LadybugDBManager
from code_visualizer.core.jobs import JobManager
from code_visualizer.tools.graph_builder import GraphBuilder
from code_visualizer.viz import changes, graph_api, server

pytest.importorskip("ladybug")
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")

STORE = """namespace App {
  public interface IStore {
    int Load(int id);
  }
  public class SqlStore : IStore {
    public int Load(int id) {
      return id;
    }
  }
}
"""
SVC = """namespace App {
  public class Svc {
    private readonly IStore _store;
    public int Run(int id) {
      return _store.Load(id);
    }
    public int Twice(int id) {
      return Run(id) * 2;
    }
    public int Legacy() {
      return 0;
    }
  }
}
"""
UTIL = """namespace App {
  public class Util {
    public int Clamp(int x) {
      return x < 0 ? 0 : x;
    }
    public int Pad(int x) {
      return x + 100;
    }
  }
}
"""
UTIL_BRANCH = """namespace App {
  public class Util {
    public int Bound(int x) {
      return x < 0 ? 0 : x;
    }
  }
}
"""
NEW = """namespace App {
  public class Fresh {
    public int Make() { return 1; }
    public int Pad(int x) {
      return x + 100;
    }
  }
}
"""
CTRL = """namespace App {
  public class Ctrl {
    private readonly Svc _svc;
    public int Get(int id) {
      return _svc.Run(id);
    }
    public int Old2() {
      return _svc.Legacy();
    }
  }
}
"""
TESTS = """namespace App.Tests {
  public class SvcTests {
    public void RunWorks() {
      var svc = new App.Svc();
      svc.Run(1);
    }
  }
}
"""
PROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Serilog" Version="3.0.0" />
  </ItemGroup>
</Project>
"""
PROJ_BRANCH = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Serilog" Version="3.1.0" />
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
  </ItemGroup>
</Project>
"""
SVC_BRANCH = """using Newtonsoft.Json;
namespace App {
  public class Svc {
    private readonly IStore _store;
    private readonly int _factor = 3;
    public int Run(int id) {
      var v = _store.Load(id);
      return v + 1;
    }
    public int Twice(int id) {
      return Run(id) * 2;
    }
    public int Thrice(int id) {
      var text = JsonConvert.SerializeObject(id);
      return Run(id) * _factor;
    }
  }
}
"""


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
                        "HOME": str(repo)})


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("changes") / "App"
    root.mkdir()
    (root / "Store.cs").write_text(STORE)
    (root / "Svc.cs").write_text(SVC)
    (root / "Ctrl.cs").write_text(CTRL)
    (root / "Old.cs").write_text("namespace App { public class Old { public void Gone() { } } }\n")
    (root / "Util.cs").write_text(UTIL)
    (root / "Tests").mkdir()
    (root / "Tests" / "SvcTests.cs").write_text(TESTS)
    (root / "App.csproj").write_text(PROJ)
    (root / ".gitignore").write_text(".code-visualizer/\n")
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "Svc.cs").write_text(SVC_BRANCH)
    (root / "Old.cs").unlink()
    (root / "New.cs").write_text(NEW)
    (root / "Util.cs").write_text(UTIL_BRANCH)
    (root / "App.csproj").write_text(PROJ_BRANCH)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feature")
    # Landed on the target after the branch left it: not a change of the branch.
    _git(root, "checkout", "-q", "develop")
    (root / "Ctrl.cs").write_text(CTRL.replace("return _svc.Run(id);", "return _svc.Run(id) + 0;"))
    _git(root, "commit", "-q", "-am", "elsewhere")
    _git(root, "checkout", "-q", "feature")
    # Not committed yet: part of the current state.
    (root / "Store.cs").write_text(STORE.replace("return id;", "return id * 10;"))

    manager = LadybugDBManager(str(root.parent / "db"))

    class _DBM:
        def get_driver(self, graph_name=None):
            return manager.get_driver()

        def get_backend_type(self):
            return "ladybugdb"

    gb = GraphBuilder(_DBM(), JobManager(), asyncio.new_event_loop())
    asyncio.new_event_loop().run_until_complete(gb.build_graph_from_path_async(root, is_dependency=False))
    yield root, manager, gb
    manager.close_driver()


def _by_name(data):
    return {n["name"]: n for n in data["nodes"]}


def test_branches_and_default_target(repo):
    root, _, _ = repo
    info = changes.branches(str(root))
    assert info["current"] == "feature"
    assert {b["name"] for b in info["branches"]} == {"develop", "feature"}
    assert info["default"] == "develop"


def test_diff_is_taken_from_the_merge_base_with_the_working_tree(repo):
    root, _, _ = repo
    diff = changes.changes(str(root), "develop")
    # The untracked .cvizignore the indexer writes is not part of the work.
    status = {f.path: f.status for f in diff["files"]}
    # Ctrl.cs changed on develop only; Store.cs is an uncommitted edit.
    assert status == {"New.cs": "added", "Old.cs": "deleted", "Store.cs": "modified", "Svc.cs": "modified",
                      "Util.cs": "modified", "App.csproj": "modified"}
    with pytest.raises(changes.GitError, match="no branch named"):
        changes.changes(str(root), "--output=/tmp/x")


def test_changed_entities_and_their_impact(repo):
    root, manager, gb = repo
    with manager.get_driver().session() as session:
        data = changes.compute(session, root.resolve().as_posix(), "develop")
    nodes = _by_name(data)
    changed = {n["name"]: n["change"] for n in data["nodes"] if n["role"] == "changed"}
    # Run edited, Thrice and Fresh/Make new, Svc changed outside its methods
    # (a field), SqlStore.Load edited (uncommitted), Util lost a method.
    # Twice and Ctrl untouched. Without the parser, nothing lost is seen, and
    # Thrice and Bound, written where Legacy and Clamp were (one git hunk),
    # pass for modified: the old version of the file is needed to tell.
    assert changed == {"Run": "modified", "Thrice": "modified", "Svc": "modified", "Pad": "added",
                       "Fresh": "added", "Make": "added", "Load": "modified", "Bound": "modified",
                       "Util": "modified"}
    callers_of_run = {l["source"] for l in data["links"] if l["target"] == nodes["Run"]["id"] and l["type"] == "CALLS"}
    assert nodes["Get"]["id"] in callers_of_run and nodes["Get"]["role"] == "caller"
    assert nodes["Twice"]["id"] in callers_of_run  # impacted, not changed
    s = data["summary"]
    assert (s["added"], s["modified"], s["deleted_files"], s["lost_detected"]) == (3, 6, 1, False)
    assert {f["rel"]: f["entities"] for f in data["files"]}["Svc.cs"] == 3


def test_deleted_moved_and_renamed_entities(repo):
    """From the merge-base version of the files: Legacy is gone, Pad moved to
    Fresh (another class, another file), Clamp became Bound with the same
    code, and the class Old went with its file and its method."""
    root, manager, gb = repo
    with manager.get_driver().session() as session:
        data = changes.compute(session, root.resolve().as_posix(), "develop", builder=gb)
    changed = {(n["name"], n["change"]) for n in data["nodes"] if n["role"] == "changed"}
    assert {("Legacy", "deleted"), ("Old", "deleted"), ("Pad", "moved"), ("Bound", "renamed")} <= changed
    assert ("Gone", "deleted") not in changed  # it went with its class
    assert ("Thrice", "added") in changed
    nodes = {n["name"]: n for n in data["nodes"] if n["role"] == "changed"}
    assert nodes["Old"]["members"] == 1 and nodes["Old"]["path"] is None and nodes["Old"]["file"] == "Old.cs"
    assert "public int Legacy()" in nodes["Legacy"]["code"] and nodes["Legacy"]["old_rel"] == "Svc.cs"
    assert nodes["Pad"]["moved_from"] == "Util.cs:6" and nodes["Pad"]["body_changed"] is False
    assert nodes["Bound"]["renamed_from"] == "Clamp" and "moved_from" not in nodes["Bound"]
    s = data["summary"]
    assert (s["deleted"], s["moved"], s["renamed"], s["added"]) == (2, 1, 1, 3)
    files = {f["rel"]: f["entities"] for f in data["files"]}
    assert files["Old.cs"] == 1 and files["Svc.cs"] == 4


def test_calls_through_the_interface_reach_the_changed_implementation(repo):
    """Svc.Run calls IStore.Load, not SqlStore.Load: the implementation's
    change reaches Run through the interface method."""
    root, manager, gb = repo
    with manager.get_driver().session() as session:
        data = changes.compute(session, root.resolve().as_posix(), "develop")
    impl = next(n for n in data["nodes"] if n["name"] == "Load" and n["role"] == "changed")
    iface = next(n for n in data["nodes"] if n["name"] == "Load" and n["role"] == "parent")
    assert iface["via_type"] == "IStore" and iface["label"] == "IStore.Load"
    assert any(l["source"] == impl["id"] and l["target"] == iface["id"] and l["rel"] == "OVERRIDES"
               for l in data["links"])
    run = next(n for n in data["nodes"] if n["name"] == "Run")
    assert any(l["source"] == run["id"] and l["target"] == iface["id"] and l.get("via") for l in data["links"])
    assert data["summary"]["via_interfaces"] == 1


def test_the_page_endpoints(repo, monkeypatch):
    root, manager, gb = repo
    monkeypatch.setattr(server, "db_manager", manager)
    monkeypatch.setitem(graph_api._index_services, "repo_root", root.resolve().as_posix())
    c = TestClient(server.app)
    info = c.get("/api/changes/branches").json()
    assert info["available"] and info["default"] == "develop"
    body = c.get("/api/changes", params={"target": "develop"}).json()
    assert body["target"] == "develop" and len(body["base"]) == 40 and body["nodes"]
    assert c.get("/api/changes", params={"target": "nope"}).status_code == 400
    assert c.post("/api/changes/fetch", json={}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_a_folder_without_git(tmp_path, monkeypatch):
    monkeypatch.setitem(graph_api._index_services, "repo_root", tmp_path.as_posix())
    body = TestClient(server.app).get("/api/changes/branches").json()
    assert body["available"] is False and body["error"]


def _alerts(repo):
    root, manager, gb = repo
    with manager.get_driver().session() as session:
        data = changes.compute(session, root.resolve().as_posix(), "develop", builder=gb)
    return data, {a["type"]: a for a in data["alerts"]}, {a["title"] for a in data["alerts"]}


def test_a_deleted_method_still_called_is_pointed_at(repo):
    data, alerts, _ = _alerts(repo)
    a = alerts["still_referenced"]
    assert a["severity"] == "warning" and a["title"].startswith("Legacy is deleted")
    [ref] = a["refs"]
    assert (ref["rel"], ref["line"], ref["text"]) == ("Ctrl.cs", 8, "return _svc.Legacy();")
    old2 = next(n for n in data["nodes"] if n["name"] == "Old2")
    legacy = next(n for n in data["nodes"] if n["name"] == "Legacy")
    assert any(l["source"] == old2["id"] and l["target"] == legacy["id"] and l["broken"] for l in data["links"])
    assert old2["uses_gone"] == ["Legacy (deleted)"]


def test_package_changes_and_types_only_the_changed_code_uses(repo):
    _, alerts, titles = _alerts(repo)
    assert "New package: Newtonsoft.Json 13.0.3" in titles
    assert "Package updated: Serilog 3.0.0 → 3.1.0" in titles
    ext = alerts["new_external"]
    assert ext["title"].startswith("JsonConvert (Newtonsoft.Json)") and ext["detail"] == "called by Thrice"


def test_changed_functions_no_test_reaches(repo):
    """SvcTests calls Run, and SqlStore.Load through IStore.Load and Run:
    both are reached; the other changed functions are not."""
    data, alerts, _ = _alerts(repo)
    untested = {n["name"] for n in data["nodes"] if n.get("untested")}
    assert untested == {"Thrice", "Make", "Pad", "Bound"}
    assert alerts["untested"]["title"] == "4 changed functions no test reaches (within 3 calls)"
