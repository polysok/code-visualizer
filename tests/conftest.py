
import os
import sys
from pathlib import Path
import tempfile
import pytest

# Tests running the CLI under a throwaway HOME would otherwise download every
# tree-sitter grammar again (the cache lives under HOME), and fail whenever
# GitHub's release CDN does. Share the developer's cache instead.
if "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR" not in os.environ:
    try:
        import tree_sitter_language_pack as _tslp

        # cache_dir() is <base>/tree-sitter-language-pack/<version>/libs
        os.environ["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(Path(_tslp.cache_dir()).parents[2])
    except Exception:
        pass

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Update the golden files with the new exported results."
    )

@pytest.fixture(autouse=True)
def _isolate_graph_events(tmp_path_factory, monkeypatch):
    """Keep graph-change notifications (and the watcher's catch-up state) out
    of the developer's ~/.code-visualizer during tests."""
    import os

    if "CVIZ_EVENTS_FILE" not in os.environ:
        base = tmp_path_factory.getbasetemp() / "cviz-events"
        base.mkdir(exist_ok=True)
        monkeypatch.setenv("CVIZ_EVENTS_FILE", str(base / "events.jsonl"))
    # Natural-language search: do not vectorize during unrelated indexing
    # tests even when the global config enables it.
    if "SEMANTIC_SEARCH" not in os.environ:
        monkeypatch.setenv("SEMANTIC_SEARCH", "false")
    # One database per repository: a test (or the CLI it spawns) run from this
    # checkout, or on a sample project inside it, would otherwise resolve to
    # <checkout>/.code-visualizer. Each test gets its own database; tests of
    # the per-repository resolution itself clear this variable.
    if "CVIZ_RUNTIME_DB_PATH" not in os.environ:
        monkeypatch.setenv("CVIZ_RUNTIME_DB_PATH", str(tmp_path_factory.mktemp("cviz-db") / "ladybugdb"))
    # Graph servers of the tests must not appear in the developer's registry.
    if "CVIZ_SERVERS_FILE" not in os.environ:
        monkeypatch.setenv("CVIZ_SERVERS_FILE", str(tmp_path_factory.getbasetemp() / "servers.json"))
    # An MCP server built by a test must not start a real `cviz graph` in the
    # background (tests of that start-up set it back to 1).
    if "CVIZ_MCP_START_GRAPH" not in os.environ:
        monkeypatch.setenv("CVIZ_MCP_START_GRAPH", "0")


@pytest.fixture
def update_goldens(request):
    return request.config.getoption("--update-goldens")

# Root of the test folder
TEST_ROOT = Path(__file__).parent.absolute()
FIXTURES_DIR = TEST_ROOT / "fixtures"
SAMPLE_PROJECTS_DIR = FIXTURES_DIR / "sample_projects"

@pytest.fixture(scope="session")
def sample_projects_path():
    """Returns the path to the directory containing all sample projects."""
    if not SAMPLE_PROJECTS_DIR.exists():
        pytest.fail(f"Sample projects directory not found at {SAMPLE_PROJECTS_DIR}")
    return SAMPLE_PROJECTS_DIR

@pytest.fixture(scope="session")
def python_sample_project(sample_projects_path):
    """Returns path to the Python sample project."""
    path = sample_projects_path / "sample_project" # Confusing name in old tests, it was just "sample_project"
    if not path.exists():
        pytest.fail(f"Python sample project not found at {path}")
    return path

@pytest.fixture(scope="session")
def javascript_sample_project(sample_projects_path):
    """Returns path to the JavaScript sample project."""
    path = sample_projects_path / "sample_project_javascript"
    if not path.exists():
        pytest.fail(f"JavaScript sample project not found at {path}")
    return path

@pytest.fixture
def temp_test_dir():
    """Creates a temporary directory for file operations, cleaned up after test."""
    with tempfile.TemporaryDirectory(prefix="cviz_test_unit_") as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def assert_parser_result():
    """Fail fast when a parser unexpectedly returns a non-dict payload."""
    def _assert(result):
        if not isinstance(result, dict):
            pytest.fail(f"Expected parser result to be a dict, got {type(result).__name__}: {result!r}")
        return result

    return _assert
