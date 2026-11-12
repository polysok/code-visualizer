# src/code_visualizer/server.py
import asyncio
import json
import sys
import traceback
import os
from pathlib import Path

from typing import Any, Dict, Optional, List, Set

from .utils.gcf_encoder import encode_response

from .prompts import LLM_SYSTEM_PROMPT, build_system_prompt
from .core import get_database_manager
from .core.jobs import JobManager
from .tools.graph_builder import GraphBuilder
from .tools.code_finder import CodeFinder
from .utils.debug_log import error_logger, warning_logger, debug_logger
from .cli.config_manager import (
    MCP_SERVER_KEY,
    resolve_context,
)

# Import Tool Definitions and Handlers
from .tool_definitions import TOOLS
from .tools.handlers import indexing_handlers, management_handlers

DEFAULT_EDIT_DISTANCE = 2
DEFAULT_FUZZY_SEARCH = False

WORKSPACE_PREFIX = "/workspace/"


def _teardown_db_manager(db_manager) -> None:
    """Release DB resources when switching contexts."""
    db_manager.close_driver()


def _is_path_key(key: str) -> bool:
    """Check if a dict key represents a file path field.

    Matches keys like 'path', 'clone_path', 'caller_file_path', and also
    Cypher-aliased keys like 'f.path', 'n.caller_file_path'.
    """
    # Strip Cypher alias prefix (e.g. "f.path" -> "path")
    bare = key.rsplit(".", 1)[-1] if "." in key else key
    return bare == "path" or bare.endswith("_path")


def _strip_path_value(value):
    """Strip /workspace/ prefix from a single string value."""
    if isinstance(value, str) and value.startswith(WORKSPACE_PREFIX):
        return value[len(WORKSPACE_PREFIX):]
    return value


def _strip_workspace_prefix(obj):
    """Recursively strip /workspace/ prefix from path values in results."""
    if isinstance(obj, dict):
        return {
            k: _strip_path_value(v) if _is_path_key(k) else _strip_workspace_prefix(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_strip_workspace_prefix(item) for item in obj]
    return obj


# Approximate chars-per-token used for budget conversion.
# GPT-family tokenizers average ~4 chars/token; using 4 is a safe conservative estimate.
_CHARS_PER_TOKEN = 4


def _effective_char_limit(max_tokens: int, max_chars_direct: int) -> int:
    """Return the effective character budget from both config knobs.

    Picks the stricter (smallest) non-zero value.  Returns 0 when both
    are zero, meaning no limit is active.
    """
    candidates = [c for c in (max_tokens * _CHARS_PER_TOKEN, max_chars_direct) if c > 0]
    return min(candidates) if candidates else 0


def _limit_source_label(max_tokens: int, max_chars_direct: int, effective: int) -> str:
    """Human-readable description of which config key produced *effective*."""
    token_chars = max_tokens * _CHARS_PER_TOKEN
    both_active = token_chars > 0 and max_chars_direct > 0
    if both_active:
        winner = "MAX_TOOL_RESPONSE_TOKENS" if token_chars <= max_chars_direct else "MAX_PROMPT_CHARS"
        return f"{winner} ({effective} chars)"
    if token_chars > 0:
        return f"MAX_TOOL_RESPONSE_TOKENS ({max_tokens} tokens → {effective} chars)"
    return f"MAX_PROMPT_CHARS ({effective} chars)"


def _apply_response_token_limit(tool_name: str, text: str) -> str:
    """Truncate *text* to the configured budget and append a visible notice.

    Reads two independent config knobs at call time so live changes are
    respected without a server restart:

    * ``MAX_TOOL_RESPONSE_TOKENS`` — token-based cap (4 chars/token)
    * ``MAX_PROMPT_CHARS``         — direct character cap

    The stricter non-zero value wins.  When both are 0 the text is returned
    unchanged.  A structured warning is printed to stdout whenever truncation
    fires so operators can trace oversized payloads.
    """
    from .cli.config_manager import get_config_value

    raw_tokens = get_config_value("MAX_TOOL_RESPONSE_TOKENS") or "0"
    try:
        max_tokens = int(raw_tokens)
    except ValueError:
        max_tokens = 0

    raw_chars = get_config_value("MAX_PROMPT_CHARS") or "0"
    try:
        max_chars_direct = int(raw_chars)
    except ValueError:
        max_chars_direct = 0

    max_chars = _effective_char_limit(max_tokens, max_chars_direct)
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    label = _limit_source_label(max_tokens, max_chars_direct, max_chars)
    notice = (
        f"[CViz] Response truncated: output exceeded {label} "
        f"for tool '{tool_name}'. "
        "Increase the limit or narrow your query for full results."
    )

    print(
        f"[CViz WARNING] Truncation fired | tool={tool_name} | "
        f"original_chars={len(text)} | truncated_chars={max_chars} | "
        f"limit={label}",
        file=sys.stderr,
        flush=True,
    )

    budget = max(0, max_chars - 200)
    try:
        payload = json.loads(text)
        preview = json.dumps(payload, indent=2)
        if len(preview) <= budget:
            return preview
        return json.dumps(
            {"truncated": True, "preview": preview[:budget], "notice": notice},
            indent=2,
        )
    except json.JSONDecodeError:
        return json.dumps(
            {"truncated": True, "preview": text[:budget], "notice": notice},
            indent=2,
        )


def render_result(tool_name: str, result: Dict[str, Any]) -> str:
    """The text the assistant reads: the tools' own text, else the encoded result."""
    if isinstance(result, dict) and set(result) == {"text"}:
        text = str(result["text"])
        from .cli.config_manager import get_config_value

        def _int(key: str) -> int:
            try:
                return int(get_config_value(key) or "0")
            except ValueError:
                return 0

        limit = _effective_char_limit(_int("MAX_TOOL_RESPONSE_TOKENS"), _int("MAX_PROMPT_CHARS"))
        if limit and len(text) > limit:
            text = (text[:max(0, limit - 200)]
                    + f"\n[CViz] Response truncated at {limit} characters: narrow the request "
                      "(fewer ids, smaller depth or limit).")
        return text
    return _apply_response_token_limit(tool_name, encode_response(result))


class MCPServer:
    """The MCP server: code-graph tools for an AI assistant, over JSON-RPC on stdio.

    Five tools (see tool_definitions): ``graph_overview``, ``search_code``,
    ``traverse_graph``, ``read_code`` and ``execute_cypher_query``.

    LadybugDB accepts a single process. The MCP server therefore never opens
    the database: it goes through the ``cviz graph`` server of the repository
    (``/api/agent/*``), and starts one in the background when none runs
    (``cviz graph --watch <repo> --no-browser``, which indexes a repository
    seen for the first time). The page and the assistant share it, and
    neither can lock the other out. ``CVIZ_MCP_START_GRAPH=0`` restores the
    former behaviour (the MCP server opens the database itself).
    """

    # How long a tool call waits for the graph server to answer (start-up,
    # first indexing of a repository).
    GRAPH_START_TIMEOUT = 120.0

    def __init__(self, loop=None, cwd: Path | None = None):
        self.cwd = (cwd or Path.cwd()).resolve()
        self.disabled_tools: Set[str] = set()
        self.proxy_url: Optional[str] = None
        self.db_manager = None
        self.graph_builder = None
        self.code_finder = None
        self._search_engine = None
        self.job_manager = JobManager()

        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        self.loop = loop

        try:
            ctx = resolve_context(cwd=self.cwd)
        except ValueError as e:
            raise ValueError(f"Database configuration error: {e}")
        self.resolved_context = ctx
        if ctx.database and not os.environ.get('CVIZ_RUNTIME_DB_TYPE'):
            os.environ['CVIZ_RUNTIME_DB_TYPE'] = ctx.database

        self._graph_process = None
        running = self._graph_server_url()
        if running:
            # cviz graph holds the database: go through it.
            self.proxy_url = running
        elif self._autostart_graph():
            # No server yet: start one and go through it; the tool calls wait
            # for it to answer, the handshake does not.
            self.proxy_url = None
            self._starting = True
            self._graph_process = self._start_graph_server()
        else:
            self.db_manager = get_database_manager(db_path=ctx.db_path)
            try:
                self.db_manager.get_driver()
            except Exception as exc:
                running = self._graph_server_url() if "lock" in str(exc).lower() else None
                if not running:
                    raise
                self.db_manager = None
                self.proxy_url = running
        if self.db_manager is not None:
            self.graph_builder = GraphBuilder(self.db_manager, self.job_manager, loop)
            self.code_finder = CodeFinder(self.db_manager)

        self._init_tools()

    _starting = False

    def _repo_root(self) -> Path:
        return Path(getattr(self.resolved_context, "repo_root", "") or self.cwd)

    @staticmethod
    def _autostart_graph() -> bool:
        return (os.environ.get("CVIZ_MCP_START_GRAPH") or "1").strip().lower() not in ("0", "false", "no")

    def _graph_log(self) -> Path:
        return self._repo_root() / ".code-visualizer" / "graph-server.log"

    def _start_graph_server(self):
        """Start ``cviz graph --watch`` for the repository, detached, unless another
        MCP server is already starting it (two IDE windows opening together)."""
        import subprocess
        import time as _time

        root = self._repo_root()
        marker = root / ".code-visualizer" / "graph-server.starting"
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                fresh = _time.time() - marker.stat().st_mtime < self.GRAPH_START_TIMEOUT
            except OSError:
                fresh = False
            if fresh:
                return None  # someone else starts it: wait for it
            marker.unlink(missing_ok=True)
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)

        command = [sys.executable, "-m", "code_visualizer.cli.main", "graph",
                   "--watch", str(root), "--no-browser"]
        log = open(self._graph_log(), "ab")
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # outlives this MCP server, shared with the page
        try:
            process = subprocess.Popen(command, cwd=str(root), stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, **kwargs)
        finally:
            log.close()
        print(f"Started the graph server of {root} (log: {self._graph_log()})", file=sys.stderr, flush=True)
        return process

    def _answers(self, url: str) -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/graph-config", timeout=2) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _ensure_graph_server(self) -> Optional[str]:
        """Error message, or None once ``self.proxy_url`` answers."""
        import time as _time

        if self.proxy_url and (not self._starting or self._answers(self.proxy_url)):
            self._starting = False
            return None
        deadline = _time.monotonic() + self.GRAPH_START_TIMEOUT
        while _time.monotonic() < deadline:
            url = self._graph_server_url()
            if url and self._answers(url):
                self.proxy_url, self._starting = url, False
                (self._repo_root() / ".code-visualizer" / "graph-server.starting").unlink(missing_ok=True)
                return None
            process = self._graph_process
            if process is not None and process.poll() is not None:
                (self._repo_root() / ".code-visualizer" / "graph-server.starting").unlink(missing_ok=True)
                self._graph_process = None
                return (f"The graph server of {self._repo_root()} stopped (exit code {process.returncode}); "
                        f"see {self._graph_log()}.")
            if not url and process is None and not self._start_marker_fresh():
                self._graph_process = self._start_graph_server()  # nobody is starting it any more
            _time.sleep(0.5)
        return (f"The graph server of {self._repo_root()} is still starting (indexing the repository "
                f"for the first time?); retry in a moment. Log: {self._graph_log()}")

    def _start_marker_fresh(self) -> bool:
        import time as _time

        marker = self._repo_root() / ".code-visualizer" / "graph-server.starting"
        try:
            return _time.time() - marker.stat().st_mtime < self.GRAPH_START_TIMEOUT
        except OSError:
            return False

    def _graph_server_url(self) -> Optional[str]:
        """URL of a live ``cviz graph`` server for this repository, if any."""
        from .cli import server_registry

        root = getattr(self.resolved_context, "repo_root", "") or str(self.cwd)
        try:
            return server_registry.server_for(root)
        except Exception:  # noqa: BLE001 - no registry: no server
            return None

    def _init_tools(self):
        """The tool manifest for the LLM (minus the ones mcp.json disables)."""
        self.disabled_tools = self._load_disabled_tools()
        self.tools = {
            name: definition
            for name, definition in TOOLS.items()
            if name not in self.disabled_tools
        }

    def _normalize_tool_name(self, name: Any) -> Optional[str]:
        """Normalize tool names from mcp.json to internal tool identifiers."""
        if not isinstance(name, str):
            return None
        normalized = name.strip()
        if not normalized:
            return None
        # Clients may prefix tool names with the server entry.
        for prefix in ("code_visualizer_", "code-visualizer_"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        return normalized

    def _load_disabled_tools(self) -> Set[str]:
        """Load disabled tool names from `<cwd>/mcp.json` config."""
        mcp_file = self.cwd / "mcp.json"
        if not mcp_file.exists():
            return set()
        try:
            with open(mcp_file, "r", encoding="utf-8") as f:
                mcp_config = json.load(f)
        except Exception as exc:
            warning_logger(f"Failed to read {mcp_file}: {exc}")
            return set()

        disabled_tools = (
            mcp_config
            .get("mcpServers", {})
            .get(MCP_SERVER_KEY, {})
            .get("tools", {})
            .get("disabledTools", [])
        )
        if not isinstance(disabled_tools, list):
            warning_logger("mcp.json tools.disabledTools must be a list; ignoring invalid value.")
            return set()

        normalized_disabled: Set[str] = set()
        unknown_tools: List[str] = []
        for name in disabled_tools:
            normalized_name = self._normalize_tool_name(name)
            if not normalized_name:
                continue
            if normalized_name in TOOLS:
                normalized_disabled.add(normalized_name)
            else:
                unknown_tools.append(str(name))
        if unknown_tools:
            warning_logger(
                "Ignoring unknown tools in mcp.json tools.disabledTools: "
                + ", ".join(sorted(set(unknown_tools)))
            )
        return normalized_disabled

    def _get_version(self) -> str:
        from .utils.package_info import installed_version

        return installed_version() or "0.0.0-dev"

    def get_database_status(self) -> dict:
        if self.db_manager is None:
            return {"connected": bool(self.proxy_url), "via": self.proxy_url}
        return {"connected": self.db_manager.is_connected()}

    # --- Not MCP tools: used by the REST API (api/router.py) ---------------

    def list_indexed_repositories_tool(self, **args) -> Dict[str, Any]:
        if self.code_finder is None:
            return {"error": f"The database is held by the graph server at {self.proxy_url}."}
        return management_handlers.list_indexed_repositories(self.code_finder, **args)

    def add_code_to_graph_tool(self, **args) -> Dict[str, Any]:
        if self.graph_builder is None:
            return {"error": f"The database is held by the graph server at {self.proxy_url}: "
                             "index from there (cviz graph --watch indexes on its own)."}
        return indexing_handlers.add_code_to_graph(
            self.graph_builder,
            self.job_manager,
            self.loop,
            self.list_indexed_repositories_tool,
            repo_root=getattr(self.resolved_context, "repo_root", "") or None,
            **args
        )

    # --- MCP tools ------------------------------------------------------------

    def _call_direct(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        from . import agent_tools

        if self._search_engine is None:
            from .semantic.search import SearchEngine

            manager = self.db_manager
            self._search_engine = SearchEngine(lambda: manager.get_driver().session())
        return agent_tools.call_tool(self.db_manager, tool_name, args, engine=self._search_engine)

    def _call_proxy(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{self.proxy_url.rstrip('/')}/api/agent/{tool_name}",
            data=json.dumps(args).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except Exception:  # noqa: BLE001
                detail = None
            return {"error": f"Graph server {self.proxy_url}: {detail or exc}"}
        except (urllib.error.URLError, OSError) as exc:
            return {"_unreachable": True,
                    "error": f"The graph server {self.proxy_url} does not answer ({exc}). "
                             "Restart it: cviz graph --watch ."}

    async def handle_tool_call(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run a tool for the AI assistant."""
        if tool_name in self.disabled_tools:
            return {"error": f"Tool '{tool_name}' is disabled in mcp.json (disabledTools)."}
        internal = {
            # The REST API's /repositories and /index (api/router.py); not offered
            # to the assistant, which neither indexes nor manages repositories.
            "list_indexed_repositories": self.list_indexed_repositories_tool,
            "add_code_to_graph": self.add_code_to_graph_tool,
        }
        if tool_name in internal:
            if "repo_path" in (args or {}) and "path" not in args:
                args = {**args, "path": args["repo_path"]}
            return await asyncio.to_thread(internal[tool_name], **(args or {}))
        if tool_name not in TOOLS:
            return {"error": f"Unknown tool: {tool_name}. Tools: {', '.join(TOOLS)}."}
        args = dict(args) if isinstance(args, dict) else {}
        if self.db_manager is not None:
            return await asyncio.to_thread(self._call_direct, tool_name, args)
        return await asyncio.to_thread(self._call_through_graph_server, tool_name, args)

    def _call_through_graph_server(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        problem = self._ensure_graph_server()
        if problem:
            return {"error": problem}
        result = self._call_proxy(tool_name, args)
        if result.get("_unreachable") and self._autostart_graph():
            # The server went away (stopped by the user): start it again, once.
            self.proxy_url, self._starting = None, True
            problem = self._ensure_graph_server()
            result = {"error": problem} if problem else self._call_proxy(tool_name, args)
        result.pop("_unreachable", None)
        return result

    async def run(self):
        """
        Runs the main server loop, listening for JSON-RPC requests from stdin.
        """
        # info_logger("MCP Server is running. Waiting for requests...")
        print("MCP Server is running. Waiting for requests...", file=sys.stderr, flush=True)
        loop = asyncio.get_event_loop()
        try:
            await self._run_loop(loop)
        finally:
            # Release watcher/DB resources even when stdin reaches EOF or the
            # loop exits unexpectedly.
            try:
                self.shutdown()
            except Exception as exc:
                warning_logger(f"Error during server shutdown: {exc}")

    async def _run_loop(self, loop):
        request_count = 0
        while True:
            # Reset per iteration: on a parse error `request` would otherwise
            # still hold the PREVIOUS request, and the error response would
            # reuse its id — a second response for an already-answered id,
            # which desyncs clients that match responses by id.
            request = None
            try:
                if request_count and request_count % 50 == 0:
                    self.job_manager.cleanup_old_jobs(max_age_hours=24)
                # Read a request from the standard input.
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    debug_logger("Client disconnected (EOF received). Shutting down.")
                    break
                
                request = json.loads(line.strip())
                method = request.get('method')
                params = request.get('params', {})
                request_id = request.get('id')
                request_count += 1
                
                response = {}
                # Route the request based on the JSON-RPC method.
                if method == 'initialize':
                    # Build system prompt with custom prompts if any
                    system_prompt = build_system_prompt()
                    
                    response = {
                        "jsonrpc": "2.0", "id": request_id,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "serverInfo": {
                                "name": "Code Visualizer",
                                "version": self._get_version(),
                                "systemPrompt": system_prompt,
                                "instructionsAvailable": True,
                            },
                            "capabilities": {"tools": {"listTools": True}},
                            "instructions": LLM_SYSTEM_PROMPT,
                        }
                    }
                elif method == 'tools/list':
                    # Return the list of tools defined in _init_tools.
                    response = {
                        "jsonrpc": "2.0", "id": request_id,
                        "result": {"tools": list(self.tools.values())}
                    }
                elif method == 'tools/call':
                    # Execute a tool call and return the result.
                    tool_name = params.get('name')
                    args = params.get('arguments', {})
                    result = await self.handle_tool_call(tool_name, args)
                    result = _strip_workspace_prefix(result)

                    if "error" in result:
                        response = {
                            "jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32000, "message": "Tool execution error", "data": result}
                        }
                    else:
                        response_text = render_result(tool_name, result)
                        response = {
                            "jsonrpc": "2.0", "id": request_id,
                            "result": {"content": [{"type": "text", "text": response_text}]}
                        }
                elif method == 'notifications/initialized':
                    # This is a notification, no response needed.
                    pass
                else:
                    # Handle unknown methods.
                    if request_id is not None:
                        response = {
                            "jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32601, "message": f"Method not found: {method}"}
                        }
                
                # Send the response to standard output if it's not a notification.
                if request_id is not None and response:
                    print(json.dumps(response), flush=True)

            except Exception as e:
                error_logger(f"Error processing request: {e}\n{traceback.format_exc()}")
                # JSON-RPC: parse errors respond with id null and -32700;
                # errors while handling a parsed request keep that request's id.
                request_id = request.get('id') if isinstance(request, dict) else None
                is_parse_error = not isinstance(request, dict)

                error_response = {
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {
                        "code": -32700 if is_parse_error else -32603,
                        "message": (
                            f"Parse error: {str(e)}" if is_parse_error
                            else f"Internal error: {str(e)}"
                        ),
                    },
                }
                print(json.dumps(error_response), flush=True)

    def shutdown(self):
        """Gracefully shuts down the server and its components."""
        debug_logger("Shutting down server...")
        if self.db_manager is not None:
            _teardown_db_manager(self.db_manager)
