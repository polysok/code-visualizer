"""
Regression test for: execute_cypher_query returns MCP error -32000 on FalkorDB/KuzuDB setups.

Root cause: execute_cypher_query (query_handlers.py) calls
    db_manager.get_driver().session(default_access_mode="READ")
but FalkorDBDriverWrapper.session() and LadybugDriverWrapper.session() did not
accept **kwargs, causing a TypeError that the MCP JSON-RPC layer converts to -32000.

Fix: Both wrappers now accept **kwargs and silently ignore Neo4j-specific args.

Repro queries from user report (v4.8.0):
  - MATCH (n) RETURN labels(n) AS labels, count(n) AS count ORDER BY count DESC LIMIT 20
  - MATCH (n) RETURN count(n) AS total_nodes
  - RETURN 1 AS test
"""

import threading
import pytest


# ---------------------------------------------------------------------------
# FalkorDBDriverWrapper — session(**kwargs) fix
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LadybugDriverWrapper — session(**kwargs) fix
# ---------------------------------------------------------------------------

class TestKuzuDriverWrapperSessionKwargs:
    """session() must accept and ignore Neo4j-specific kwargs."""

    def _make_wrapper(self):
        from code_visualizer.core.database_ladybug import LadybugDriverWrapper
        import inspect
        return LadybugDriverWrapper, inspect

    def test_session_signature_accepts_kwargs(self):
        """LadybugDriverWrapper.session must have **kwargs in its signature."""
        from code_visualizer.core.database_ladybug import LadybugDriverWrapper
        import inspect
        sig = inspect.signature(LadybugDriverWrapper.session)
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        assert has_var_keyword, (
            f"LadybugDriverWrapper.session() is missing **kwargs. "
            f"Calling it with default_access_mode='READ' will raise TypeError → MCP -32000. "
            f"Actual signature: {sig}"
        )

    def test_session_call_with_default_access_mode_does_not_raise(self):
        """session(default_access_mode='READ') must not raise TypeError."""
        from code_visualizer.core.database_ladybug import LadybugDriverWrapper
        from unittest.mock import MagicMock

        mock_conn = MagicMock()
        lock = threading.RLock()
        wrapper = LadybugDriverWrapper.__new__(LadybugDriverWrapper)
        wrapper.conn = mock_conn
        wrapper._query_lock = lock

        # Must not raise TypeError
        try:
            session = wrapper.session(default_access_mode="READ")
        except TypeError as e:
            pytest.fail(
                f"LadybugDriverWrapper.session(default_access_mode='READ') raised TypeError: {e}\n"
                "This causes MCP -32000 for all execute_cypher_query calls on KuzuDB backends."
            )
