"""Type-name normalisation for TypeScript / Swift receiver inference."""
import pytest

from code_visualizer.tools.languages import member_types as mt


@pytest.mark.parametrize(
    ("lang", "text", "expected"),
    [
        ("typescript", ": Map<string, number>", "Map"),
        ("typescript", "Store | null", "Store"),
        ("typescript", "Store | Repo", None),          # ambiguous union
        ("typescript", "readonly Store[]", "Array"),
        ("typescript", "ns.inner.Store", "Store"),
        ("typescript", "{ a: number }", None),
        ("typescript", "string", "string"),            # primitive: external, no edge
        ("swift", "Store?", "Store"),
        ("swift", "[Int]", "Array"),
        ("swift", "[String: Int]", "Dictionary"),
        ("swift", "some Repo", "Repo"),
        ("swift", "Foundation.NSLock", "NSLock"),
        ("swift", "(Int) -> Void", None),
    ],
)
def test_type_name(lang, text, expected):
    assert mt.type_name(lang, text) == expected


def test_await_unwraps_promise():
    assert mt.unwrap_await("typescript", "Promise<Store>") == "Store"
    assert mt.unwrap_await("typescript", "Store") == "Store"


def test_chain_reads_fields_across_files(tmp_path):
    holder = tmp_path / "holder.ts"
    holder.write_text("export class Holder {\n  inner: Store;\n  constructor(private readonly repo: Repo) {}\n}\n")
    imports_map = {"Holder": [str(holder)]}
    chain = {"lang": "typescript", "base": "Holder", "steps": [["field", "inner"]]}
    assert mt.evaluate_receiver(chain, imports_map) == "Store"
    chain["steps"] = [["field", "repo"]]
    assert mt.evaluate_receiver(chain, imports_map) == "Repo"     # constructor parameter property
    chain["steps"] = [["field", "missing"]]
    assert mt.evaluate_receiver(chain, imports_map) is None

    swift = tmp_path / "Holder.swift"
    swift.write_text("class Holder {\n  let inner: Store?\n  var made = Maker()\n}\n")
    chain = {"lang": "swift", "base": "Holder", "steps": [["field", "inner"]]}
    assert mt.evaluate_receiver(chain, {"Holder": [str(swift)]}) == "Store"
    chain["steps"] = [["field", "made"]]
    assert mt.evaluate_receiver(chain, {"Holder": [str(swift)]}) == "Maker"
