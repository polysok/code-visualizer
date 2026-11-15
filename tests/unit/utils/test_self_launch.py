"""utils/self_launch: relaunching cviz from a Python install or the frozen binary."""
import sys

from code_visualizer.utils import self_launch


def test_cviz_argv_runs_the_cli_module_from_python(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert self_launch.cviz_argv("graph", "--watch", "/r") == [
        sys.executable, "-m", "code_visualizer.cli.main", "graph", "--watch", "/r"]


def test_cviz_argv_runs_the_binary_itself_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert self_launch.cviz_argv("graph") == [sys.executable, "graph"]


def test_detached_env_gives_the_child_its_own_bundle(monkeypatch):
    monkeypatch.setenv("CVIZ_X", "1")
    env = self_launch.detached_env()
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and env["CVIZ_X"] == "1"
