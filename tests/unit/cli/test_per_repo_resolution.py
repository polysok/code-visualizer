"""One database per repository: how a path is mapped to its repository and database."""
import os
import subprocess

import pytest

from code_visualizer.cli import config_manager, server_registry


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".code-visualizer").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(config_manager, "CONFIG_DIR", home / ".code-visualizer")
    monkeypatch.delenv("CVIZ_RUNTIME_DB_PATH", raising=False)
    return home


def _git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_a_subfolder_uses_the_git_repository_database(home):
    repo = _git_repo(home / "work" / "proj")
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)

    ctx = config_manager.resolve_context(cwd=sub)

    assert ctx.repo_root == repo.resolve().as_posix()
    assert ctx.db_path == str(repo.resolve() / ".code-visualizer" / "db" / "ladybugdb")
    assert ctx.mode == "per-repo"
    # never versioned, without touching the project's .gitignore
    assert (repo / ".code-visualizer" / ".gitignore").read_text().splitlines()[-1] == "*"
    assert not (repo / ".gitignore").exists()


def test_an_existing_index_folder_wins_over_the_git_root(home):
    repo = _git_repo(home / "mono")
    module = repo / "services" / "api"
    (module / ".code-visualizer").mkdir(parents=True)
    ctx = config_manager.resolve_context(cwd=module / "app.py")
    assert ctx.repo_root == module.resolve().as_posix()


def test_a_folder_outside_git_is_its_own_repository(home):
    folder = home / "scratch"
    folder.mkdir()
    assert config_manager.resolve_context(cwd=folder).repo_root == folder.resolve().as_posix()


def test_the_home_folder_is_refused(home):
    with pytest.raises(config_manager.NotInAProjectError):
        config_manager.resolve_context(cwd=home)


def test_runtime_path_overrides_the_location(home, monkeypatch):
    repo = _git_repo(home / "r")
    monkeypatch.setenv("CVIZ_RUNTIME_DB_PATH", str(home / "elsewhere" / "db"))
    ctx = config_manager.resolve_context(cwd=repo)
    assert ctx.db_path == str(home / "elsewhere" / "db")
    assert ctx.repo_root == repo.resolve().as_posix()


def test_server_registry_finds_the_server_of_a_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("CVIZ_SERVERS_FILE", str(tmp_path / "servers.json"))
    a, b = tmp_path / "a", tmp_path / "a" / "nested"
    b.mkdir(parents=True)
    server_registry.register(a, "http://localhost:9324/")
    server_registry.register(b, "http://localhost:9325", pid=os.getpid())

    assert server_registry.server_for(a / "x.py") == "http://localhost:9324"
    assert server_registry.server_for(b / "y.py") == "http://localhost:9325"  # most specific
    assert server_registry.server_for(tmp_path / "other.py") is None

    server_registry.unregister(b)
    assert server_registry.server_for(b / "y.py") == "http://localhost:9324"


def test_server_registry_drops_dead_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("CVIZ_SERVERS_FILE", str(tmp_path / "servers.json"))
    proc = subprocess.run(["true"])  # noqa: S603 - a pid that no longer exists
    dead = os.getpid() + 999_999
    server_registry.register(tmp_path, "http://localhost:9999", pid=dead)
    assert server_registry.server_for(tmp_path / "f.py") is None
    assert server_registry.servers() == {}
    assert proc.returncode == 0


def test_free_port_skips_a_port_in_use():
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert server_registry.free_port(port) != port
