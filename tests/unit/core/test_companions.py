"""The list of companion sources of a repository (.code-visualizer/companions.json)."""
import pytest

from code_visualizer.core import companions


@pytest.fixture
def layout(tmp_path):
    main = tmp_path / "App"
    model = tmp_path / "Acme.Billing"
    other = tmp_path / "Unrelated"
    for d in (main, model / "src" / "Acme.Billing", other):
        d.mkdir(parents=True)
    (model / ".git").mkdir()
    (other / ".git").mkdir()
    (model / "src" / "Acme.Billing" / "Acme.Billing.csproj").write_text("<Project/>")
    (main / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Acme.Billing" Version="1.0" /></ItemGroup></Project>')
    return main, model, other


def test_add_relative_path_then_remove(layout):
    main, model, _ = layout
    added = companions.add(main, "../Acme.Billing")
    assert added == model.resolve().as_posix()
    assert companions.companion_roots(main) == [model.resolve()]
    described = companions.describe(main)
    assert [(s["name"], s["kind"], s["indexed"]) for s in described["sources"]] == [
        ("Acme.Billing", "repository", False)]
    assert described["pending"] is True  # not in the graph until the next index
    assert companions.display_path(added, main) == "../Acme.Billing"

    companions.record_indexed(main, [model])
    assert companions.describe(main)["pending"] is False

    assert companions.remove(main, added) is True
    described = companions.describe(main)
    assert described["sources"] == [] and described["removed_pending"] == [added]
    assert described["pending"] is True  # still in the graph until re-indexed
    assert (main / ".code-visualizer" / ".gitignore").is_file()  # never versioned


@pytest.mark.parametrize(("path", "reason"), [
    ("", "Give the path"),
    ("../Nowhere", "does not exist"),
    ("App.csproj", "is a file"),
    (".", "this repository"),
    ("..", "inside or around this repository"),
])
def test_refused_paths(layout, path, reason):
    main, _, _ = layout
    with pytest.raises(companions.CompanionError, match=reason):
        companions.add(main, path)


def test_overlapping_sources_are_refused(layout):
    main, model, _ = layout
    companions.add(main, str(model))
    with pytest.raises(companions.CompanionError, match="already covered"):
        companions.add(main, str(model / "src"))


def test_missing_folder_is_listed_but_not_indexed(layout):
    main, model, _ = layout
    companions.add(main, str(model))
    (model / ".git").rmdir()
    for p in sorted(model.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    model.rmdir()
    assert companions.companion_roots(main) == []
    [source] = companions.describe(main)["sources"]
    assert (source["exists"], source["kind"]) == (False, "missing")


def test_suggestions_put_package_providers_first(layout):
    main, model, other = layout
    out = companions.suggestions(main)
    assert out[0]["name"] == "Acme.Billing" and out[0]["provides"] == ["Acme.Billing"]
    assert "Unrelated" not in [s["name"] for s in out]  # a provider exists: only providers
    companions.add(main, str(model))
    assert all(s["name"] != "Acme.Billing" for s in companions.suggestions(main))
