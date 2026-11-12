"""A call's context must name its caller at the line of the caller's graph node.

Function nodes are recorded at their name's line, after attributes such as
[HttpGet] or [Fact]; the context used to give the declaration's first line
(the attribute), so the writer found no caller node and dropped the call:
controller actions and xUnit tests had no outgoing CALLS at all.
"""
from unittest.mock import MagicMock

from code_visualizer.tools.languages.csharp import CSharpTreeSitterParser
from code_visualizer.utils.tree_sitter_manager import get_tree_sitter_manager

SOURCE = """namespace Demo {
  [ApiController]
  public class OrdersController {
    private readonly Repo _repo;
    public OrdersController(Repo repo)
    {
      _repo = repo;
      Init();
    }
    [HttpGet("x")]
    [ProducesResponseType(200)]
    public int Get(int id)
    {
      return Helper(id);
    }
    private void Init() {}
    private int Helper(int id) { return id; }
  }
}
"""


def _parse(tmp_path):
    manager = get_tree_sitter_manager()
    wrapper = MagicMock()
    wrapper.language_name = "c_sharp"
    wrapper.language = manager.get_language_safe("c_sharp")
    wrapper.parser = manager.create_parser("c_sharp")
    path = tmp_path / "OrdersController.cs"
    path.write_text(SOURCE, encoding="utf-8")
    return CSharpTreeSitterParser(wrapper).parse(path)


def test_call_context_line_is_the_caller_node_line(tmp_path):
    result = _parse(tmp_path)
    lines = {f["name"]: f["line_number"] for f in result["functions"]}
    calls = {c["name"]: c for c in result["function_calls"]}

    # In an attributed method: context at the method name's line, not [HttpGet]'s.
    assert lines["Get"] == 12
    assert calls["Helper"]["context"] == ("Get", "method_declaration", 12)
    # In a constructor: the constructor, not the class.
    assert calls["Init"]["context"] == ("OrdersController", "constructor_declaration", lines["OrdersController"])
    # The class context keeps the type's line (its graph node starts at [ApiController]).
    assert calls["Helper"]["class_context"] == ("OrdersController", 2)
    assert {c["line_number"] for c in result["classes"] if c["name"] == "OrdersController"} == {2}
