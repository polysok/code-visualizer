"""Rust receiver-type inference (tools/languages/rust_types.py).

`run.control.lock()` on a `std::sync::Mutex` used to be resolved by name alone
to a project method called `lock`. The parser now describes the receiver
(base type + field/method steps) and the resolver evaluates it across files.
"""
from unittest.mock import MagicMock

import pytest

from code_visualizer.tools.languages import rust_types
from code_visualizer.tools.languages.rust import RustTreeSitterParser
from code_visualizer.utils.tree_sitter_manager import get_tree_sitter_manager


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("State<'_, PipelineRun>", ("State", ["PipelineRun"])),
        ("&'a mut tokio::sync::Mutex<Option<Session>>", ("Mutex", ["Option<Session>"])),
        ("Box<dyn Embedder + Send>", ("Box", ["dyn Embedder + Send"])),
        ("HashMap<String, Vec<u8>>", ("HashMap", ["String", "Vec<u8>"])),
        ("(i32, u8)", (None, [])),
        ("[u8; 4]", (None, [])),
    ],
)
def test_parse_type(text, expected):
    assert rust_types.parse_type(text) == expected


def test_simple_name_derefs_wrappers_and_self():
    assert rust_types.simple_name("State<'_, Arc<Run>>") == "Run"
    assert rust_types.simple_name("Box<dyn Embedder + Send>") == "Embedder"
    assert rust_types.simple_name("Mutex<Control>") == "Mutex"  # not auto-deref
    assert rust_types.simple_name("Self", self_type="Store") == "Store"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "run.rs").write_text(
        "use std::sync::Mutex;\n"
        "pub struct PipelineRun { control: Mutex<Control>, session: tokio::sync::Mutex<Option<u8>> }\n"
        "struct Control { cancel: CancelToken, stop: bool }\n",
        encoding="utf-8",
    )
    (tmp_path / "cmd.rs").write_text(
        "pub async fn stop(run: State<'_, PipelineRun>) {\n"
        "    let mut control = run.control.lock().unwrap();\n"
        "    control.cancel.cancel();\n"
        "    run.control.lock().unwrap().cancel.cancel();\n"
        "    *run.session.lock().await = None;\n"
        "    let token = CancelToken::new();\n"
        "    token.cancel();\n"
        "    let token = token.clone();\n"
        "    token.cancel();\n"
        "    unknown.cancel();\n"
        "    run.control.lock().unwrap().0.cancel();\n"
        "}\n"
        "impl Store {\n"
        "    fn save(&self) { self.embedder.embed(); }\n"
        "}\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def calls(repo):
    manager = get_tree_sitter_manager()
    wrapper = MagicMock()
    wrapper.language_name = "rust"
    wrapper.language = manager.get_language_safe("rust")
    wrapper.parser = manager.create_parser("rust")
    result = RustTreeSitterParser(wrapper).parse(repo / "cmd.rs")
    return [c for c in result["function_calls"]]


def _receiver(calls, line, name):
    call = next(c for c in calls if c["line_number"] == line and c["name"] == name)
    if call.get("inferred_obj_type"):
        return call["inferred_obj_type"]
    return call.get("receiver_chain")


def test_parser_describes_receivers(calls):
    assert _receiver(calls, 2, "lock") == {"base": "State<'_, PipelineRun>", "self_type": None,
                                           "steps": [["field", "control"]]}
    assert _receiver(calls, 3, "cancel")["steps"] == [
        ["field", "control"], ["call", "lock"], ["call", "unwrap"], ["field", "cancel"]]
    assert _receiver(calls, 7, "cancel") == "CancelToken"           # let x = T::new()
    assert _receiver(calls, 9, "cancel")["steps"] == [["call", "clone"]]  # shadowed let
    assert _receiver(calls, 10, "cancel") is None                    # unknown binding
    assert _receiver(calls, 11, "cancel") is None                    # tuple field `.0`
    assert _receiver(calls, 14, "embed") == {"base": "Store", "self_type": "Store",
                                             "steps": [["field", "embedder"]]}


def test_chain_evaluation_across_files(calls, repo):
    imports_map = {"PipelineRun": [str(repo / "run.rs")], "Control": [str(repo / "run.rs")]}

    def evaluate(line, name):
        return rust_types.evaluate_receiver(_receiver(calls, line, name), imports_map)

    assert evaluate(2, "lock") == "Mutex"            # std Mutex: an external call
    assert evaluate(3, "cancel") == "CancelToken"    # through the guard to the field
    assert evaluate(4, "cancel") == "CancelToken"
    assert evaluate(5, "lock") == "Mutex"
    assert evaluate(9, "cancel") == "CancelToken"
    # A field of an unknown struct stays unknown (previous behaviour kept).
    assert evaluate(14, "embed") is None
