"""Regression tests for MCP tool contracts."""
import asyncio

from code_visualizer import prompts
from code_visualizer.server import MCPServer
from code_visualizer.tool_definitions import TOOLS

EXPECTED = {"graph_overview", "search_code", "traverse_graph", "read_code", "execute_cypher_query"}


def test_the_assistant_gets_the_read_only_graph_tools_only():
    """Indexing, watching, deleting and context switching are the user's
    business (cviz graph --watch, the page): an assistant offered them
    burned turns on them and could drop data."""
    assert set(TOOLS) == EXPECTED
    for definition in TOOLS.values():
        text = definition["description"].lower()
        assert "delete" not in text and "index this" not in text


def test_tool_definition_keys_match_declared_names():
    for key, definition in TOOLS.items():
        assert definition["name"] == key


def test_every_tool_is_dispatched():
    """Every declared tool reaches agent_tools; an unknown one says which exist."""
    from code_visualizer import agent_tools

    assert set(agent_tools.TOOL_NAMES) == EXPECTED
    server = MCPServer.__new__(MCPServer)
    server.disabled_tools = set()
    server.proxy_url = None
    result = asyncio.run(server.handle_tool_call("find_code", {}))
    assert "Unknown tool" in result["error"] and "search_code" in result["error"]


def test_system_prompt_names_the_tools_it_offers():
    for name in EXPECTED:
        assert f"`{name}`" in prompts.LLM_SYSTEM_PROMPT
    for gone in ("find_code", "analyze_code_relationships", "add_code_to_graph", "watch_directory"):
        assert gone not in prompts.LLM_SYSTEM_PROMPT
