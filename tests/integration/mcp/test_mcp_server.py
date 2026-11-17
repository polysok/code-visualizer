"""The MCP server: handshake, the five graph tools, disabled tools, and the
relay through `cviz graph` when that server holds the database."""
import asyncio
import io
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from code_visualizer.server import MCPServer, render_result


@pytest.fixture
def no_graph_server(monkeypatch):
    monkeypatch.setattr(MCPServer, "_graph_server_url", lambda self: None)


def _server(cwd=None):
    with patch("code_visualizer.server.get_database_manager") as mock_get_db, \
         patch("code_visualizer.server.GraphBuilder"), \
         patch("code_visualizer.server.CodeFinder"):
        mock_get_db.return_value = MagicMock()
        return MCPServer(cwd=cwd)


def _mcp_json(tmp_path, disabled):
    (tmp_path / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"code-visualizer": {"tools": {"disabledTools": disabled}}}}), encoding="utf-8")


def test_initialize_returns_result_not_internal_error(no_graph_server, monkeypatch, capsys):
    """The stdio `initialize` handshake must return a result, not an internal error."""
    server = _server()
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "test-client", "version": "1.0"}}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    async def run():
        await server._run_loop(asyncio.get_running_loop())

    asyncio.run(run())
    response = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "error" not in response, response.get("error")
    assert "search_code" in response["result"]["instructions"]


def test_tools_route_to_agent_tools_on_the_own_database(no_graph_server):
    server = _server()
    with patch("code_visualizer.agent_tools.call_tool", return_value={"text": "tree"}) as call:
        result = asyncio.run(server.handle_tool_call("traverse_graph", {"start": ["a.py:f"]}))
    assert result == {"text": "tree"}
    assert call.call_args.args[:3] == (server.db_manager, "traverse_graph", {"start": ["a.py:f"]})


def test_removed_and_unknown_tools_are_refused(no_graph_server):
    server = _server()
    for name in ("find_code", "watch_directory", "delete_repository", "nope"):
        result = asyncio.run(server.handle_tool_call(name, {}))
        assert "Unknown tool" in result["error"]


def test_disabled_tools_are_neither_listed_nor_run(no_graph_server, tmp_path):
    _mcp_json(tmp_path, ["code_visualizer_read_code", "search_code", "find_code"])
    server = _server(cwd=tmp_path)
    assert "read_code" not in server.tools and "search_code" not in server.tools
    assert "traverse_graph" in server.tools
    result = asyncio.run(server.handle_tool_call("search_code", {"query": "x"}))
    assert result == {"error": "Tool 'search_code' is disabled in mcp.json (disabledTools)."}


def test_text_results_reach_the_assistant_as_text():
    assert render_result("read_code", {"text": "a.py:f  [Function]\n  1  def f(): pass"}) == (
        "a.py:f  [Function]\n  1  def f(): pass")
    assert json.loads(render_result("execute_cypher_query", {"results": [1]})) == {"results": [1]}


def test_relays_through_the_graph_server_holding_the_database(monkeypatch):
    """LadybugDB accepts one process: with `cviz graph` running, opening the
    database failed on its lock and the MCP server could not start at all."""
    monkeypatch.setattr(MCPServer, "_graph_server_url", lambda self: "http://127.0.0.1:9999")
    with patch("code_visualizer.server.get_database_manager") as mock_get_db:
        server = MCPServer()
    mock_get_db.assert_not_called()
    assert server.proxy_url == "http://127.0.0.1:9999" and server.db_manager is None

    sent = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"text": "relayed"}).encode()

    def fake_urlopen(request, timeout):
        sent["url"], sent["body"] = request.full_url, json.loads(request.data)
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = asyncio.run(server.handle_tool_call("search_code", {"query": "greet"}))
    assert result == {"text": "relayed"}
    assert sent == {"url": "http://127.0.0.1:9999/api/agent/search_code", "body": {"query": "greet"}}


def test_falls_back_to_the_graph_server_when_the_database_is_locked(monkeypatch):
    urls = iter([None, "http://127.0.0.1:9998"])  # registered while we were opening
    monkeypatch.setattr(MCPServer, "_graph_server_url", lambda self: next(urls))
    manager = MagicMock()
    manager.get_driver.side_effect = RuntimeError("IO exception: Could not set lock on file")
    with patch("code_visualizer.server.get_database_manager", return_value=manager):
        server = MCPServer()
    assert server.proxy_url == "http://127.0.0.1:9998"


def test_locked_database_without_graph_server_still_fails(monkeypatch):
    monkeypatch.setattr(MCPServer, "_graph_server_url", lambda self: None)
    manager = MagicMock()
    manager.get_driver.side_effect = RuntimeError("IO exception: Could not set lock on file")
    with patch("code_visualizer.server.get_database_manager", return_value=manager), \
         pytest.raises(RuntimeError, match="lock"):
        MCPServer()


class _FakeProcess:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode

    def wait(self):  # the reaper thread (server._start_graph_server)
        return self.returncode


def test_starts_the_graph_server_instead_of_opening_the_database(monkeypatch, tmp_path):
    """With no cviz graph running, the MCP server starts one and goes through
    it: it never holds the database, so the page can always start too."""
    monkeypatch.setenv("CVIZ_MCP_START_GRAPH", "1")
    registered = {"url": None}
    monkeypatch.setattr(MCPServer, "_graph_server_url", lambda self: registered["url"])
    started = []
    monkeypatch.setattr(MCPServer, "_start_graph_server",
                        lambda self: started.append(1) or _FakeProcess())
    monkeypatch.setattr(MCPServer, "_answers", lambda self, url: True)
    with patch("code_visualizer.server.get_database_manager") as mock_get_db:
        server = MCPServer(cwd=tmp_path)
    mock_get_db.assert_not_called()
    assert started == [1] and server.db_manager is None

    registered["url"] = "http://127.0.0.1:9997"  # the server came up
    monkeypatch.setattr(MCPServer, "_call_proxy", lambda self, name, args: {"text": self.proxy_url})
    assert asyncio.run(server.handle_tool_call("graph_overview", {})) == {"text": "http://127.0.0.1:9997"}


def test_a_graph_server_that_dies_while_starting_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("CVIZ_MCP_START_GRAPH", "1")
    monkeypatch.setattr(MCPServer, "_graph_server_url", lambda self: None)
    monkeypatch.setattr(MCPServer, "_start_graph_server", lambda self: _FakeProcess(code=1))
    server = MCPServer(cwd=tmp_path)
    result = asyncio.run(server.handle_tool_call("graph_overview", {}))
    assert "stopped (exit code 1)" in result["error"] and "graph-server.log" in result["error"]


def test_a_second_mcp_server_waits_for_the_one_starting_the_graph_server(monkeypatch, tmp_path):
    """Two IDE windows opening at once must not start two graph servers (the
    second would die on the database lock and unregister the first)."""
    monkeypatch.setenv("CVIZ_MCP_START_GRAPH", "1")
    monkeypatch.setattr(MCPServer, "_graph_server_url", lambda self: None)
    spawned = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: spawned.append(a[0]) or _FakeProcess())
    first = MCPServer(cwd=tmp_path)
    second = MCPServer(cwd=tmp_path)
    assert len(spawned) == 1 and spawned[0][-3:] == ["--watch", str(first._repo_root()), "--no-browser"]
    assert second._graph_process is None


@pytest.fixture
def two_repos(monkeypatch, tmp_path):
    """Repositories a/ and b/; the graph server is "started" without a process."""
    monkeypatch.setenv("CVIZ_MCP_START_GRAPH", "1")
    monkeypatch.setattr(MCPServer, "_graph_server_url", lambda self: None)
    monkeypatch.setattr(MCPServer, "_start_graph_server", lambda self: _FakeProcess())
    for name in ("a", "b"):
        (tmp_path / name / ".git").mkdir(parents=True)
    return tmp_path / "a", tmp_path / "b"


def _roots(*folders):
    return [{"uri": folder.resolve().as_uri(), "name": folder.name} for folder in folders]


def test_the_client_roots_choose_the_repository(two_repos):
    a, b = two_repos
    server = MCPServer(cwd=a)
    assert server._repo_root() == a.resolve()
    server._on_roots([{"uri": "https://example.com/x"}] + _roots(b))
    assert server._repo_root() == b.resolve()


def test_started_outside_a_project_it_waits_for_the_roots(two_repos, monkeypatch, tmp_path):
    """Claude Desktop starts its servers in the home folder."""
    a, _b = two_repos
    from code_visualizer.cli import config_manager

    home = tmp_path / "home"
    (home / ".code-visualizer").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(config_manager, "CONFIG_DIR", home / ".code-visualizer")
    monkeypatch.delenv("CVIZ_RUNTIME_DB_PATH", raising=False)
    server = MCPServer(cwd=home)
    result = asyncio.run(server.handle_tool_call("graph_overview", {}))
    assert "--repo" in result["error"]
    server._on_roots(_roots(a))
    assert server._repo_root() == a.resolve() and server.unbound_reason is None


def test_a_pinned_repository_ignores_the_roots(two_repos):
    a, b = two_repos
    server = MCPServer(cwd=a, pinned=True)
    server._on_roots(_roots(b))
    assert server._repo_root() == a.resolve()


def test_roots_are_asked_for_after_the_handshake(two_repos, monkeypatch, capsys):
    a, b = two_repos
    server = MCPServer(cwd=a)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "cviz-roots", "result": {"roots": _roots(b)}},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in messages)))

    async def run():
        await server._run_loop(asyncio.get_running_loop())

    asyncio.run(run())
    sent = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert {"jsonrpc": "2.0", "id": "cviz-roots", "method": "roots/list"} in sent
    assert all(m.get("id") != "cviz-roots" or "error" not in m for m in sent)  # no reply to a reply
    assert server._repo_root() == b.resolve()
