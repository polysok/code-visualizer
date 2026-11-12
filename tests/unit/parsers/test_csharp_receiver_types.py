"""C# calls carry the receiver's declared type (`inferred_obj_type`).

Without it, `IGreetingService svc = new GreetingService(); svc.Greet(u);` left
the resolver with two `Greet` candidates (interface and class) and no edge at
all, so callers of interface methods were missing from the call graph — the
dominant call shape in dependency-injected .NET code.
"""
from unittest.mock import MagicMock

import pytest

from code_visualizer.tools.languages.csharp import CSharpTreeSitterParser
from code_visualizer.utils.tree_sitter_manager import get_tree_sitter_manager

SOURCE = """using System.Collections.Generic;
namespace Demo {
  public interface IRepo { void Save(int x); }
  public class Repo : IRepo { public void Save(int x) {} }
  public class Svc {
    private readonly IRepo _repo;
    public List<int> Items { get; set; }
    public Svc(IRepo repo) { _repo = repo; }
    public void Run(Repo concrete) {
      var created = new Repo();
      IRepo typed = created;
      _repo.Save(1);
      this._repo.Save(2);
      concrete.Save(3);
      created.Save(4);
      typed.Save(5);
      Items.Add(6);
      foreach (Repo r in new List<Repo>()) { r.Save(7); }
      unknown.Save(8);
      Demo.Util.Helper.Save(9);
    }
    public void Shadow() {
      Repo _repo = new Repo();
      _repo.Save(10);
    }
  }
}
"""


@pytest.fixture(scope="module")
def calls(tmp_path_factory):
    manager = get_tree_sitter_manager()
    wrapper = MagicMock()
    wrapper.language_name = "c_sharp"
    wrapper.language = manager.get_language_safe("c_sharp")
    wrapper.parser = manager.create_parser("c_sharp")
    path = tmp_path_factory.mktemp("cs") / "Svc.cs"
    path.write_text(SOURCE, encoding="utf-8")
    result = CSharpTreeSitterParser(wrapper).parse(path)
    return {c["line_number"]: c for c in result["function_calls"] if c["name"] in ("Save", "Add")}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (12, "IRepo"),   # field
        (13, "IRepo"),   # this.field
        (14, "Repo"),    # parameter
        (15, "Repo"),    # var x = new T()
        (16, "IRepo"),   # declared interface type wins over the initializer
        (17, "List"),    # property, generic stripped
        (18, "Repo"),    # foreach variable
        (19, None),      # unknown receiver
        (20, None),      # qualified static call: not a variable
        (24, "Repo"),    # local shadows the field of the same name
    ],
)
def test_receiver_type(calls, line, expected):
    assert calls[line]["inferred_obj_type"] == expected
