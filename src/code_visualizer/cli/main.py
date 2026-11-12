# src/code_visualizer/cli/main.py
"""
This module defines the command-line interface (CLI) for the Code Visualizer application.
It uses the Typer library to create a user-friendly and well-documented CLI.

Commands:
- mcp setup: Runs an interactive wizard to configure the MCP client.
- mcp start: Launches the main MCP server.
- help: Displays help information.
- version: Show the installed version.
"""
import sys
import typer
from rich.console import Console
from rich.table import Table
from rich import box
from typing import Optional
from .open_in_graph import DEFAULT_PORT as DEFAULT_GRAPH_PORT
import asyncio
import logging
import json
import os
from pathlib import Path

from code_visualizer.server import MCPServer
from .setup_wizard import configure_mcp_client
from . import config_manager
from . import project_config
# Import the new helper functions
from .cli_helpers import (
    index_helper,
    add_package_helper,
    list_repos_helper,
    delete_helper,
    cypher_helper,
    cypher_helper_visual,
    visualize_helper,
    graph_helper,
    reindex_helper,
    update_helper,
    clean_helper,
    stats_helper,
    _initialize_services,
    watch_helper,
    unwatch_helper,
    list_watching_helper,
    setup_scip_helper,
)
from .hook_manager import HookError, get_hook_status, install_hooks, uninstall_hooks
from code_visualizer.utils.tool_limits import get_tool_result_limit

# Set the log level for the noisy asyncio and urllib3 loggers to keep the output clean.
# Get the log level from config, defaulting to WARNING
def _configure_library_loggers():
    """Configure third-party library loggers based on config setting."""
    try:
        log_level_str = config_manager.get_config_value('LIBRARY_LOG_LEVEL')
        if log_level_str is None:
            log_level_str = 'WARNING'
        log_level_str = str(log_level_str).upper()
        log_level = getattr(logging, log_level_str, logging.WARNING)
    except (AttributeError, Exception):
        log_level = logging.WARNING
    
    logging.getLogger("asyncio").setLevel(log_level)
    logging.getLogger("urllib3").setLevel(log_level)

_configure_library_loggers()


# Import visualization module
from .visualizer import (
    visualize_call_graph,
    visualize_call_chain,
    visualize_dependencies,
    visualize_inheritance_tree,
    visualize_overrides,
    visualize_search_results,
    check_visual_flag,
)

# Initialize the Typer app and Rich console for formatted output.
app = typer.Typer(
    name="cviz",
    help="Code Visualizer: An MCP server for AI-powered code analysis.",
    add_completion=True,
    # `-h` is accepted as --help on every command, not just at the root.
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console(stderr=True)
# `analyze` prints its primary product (the table, and the notice that there is
# none), so it goes to stdout like `cviz stats` / `cviz diagram` already do;
# `console` stays for interactive prompts and diagnostics. Argument validation
# errors inside those commands remain on the stderr console.
analyze_result_console = Console()
find_result_console = Console()

# Configure basic logging for the application. Default to WARNING so CLI
# output stays clean; the root --debug flag switches this to DEBUG.
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')


def get_version() -> str:
    """
    Try to read version from the installed package metadata.
    Fallback to a dev version if not installed.
    """
    from code_visualizer.utils.package_info import installed_version

    return installed_version() or "0.0.0 (dev)"


# Create MCP command group
mcp_app = typer.Typer(help="MCP client configuration commands")
app.add_typer(mcp_app, name="mcp")

@mcp_app.command("setup")
def mcp_setup():
    """
    Configure MCP Client (IDE/CLI Integration).
    
    Sets up Code Visualizer integration with your IDE or CLI tool:
    - VS Code, Cursor, Windsurf, Zed
    - Claude Desktop, Gemini CLI
    - Cline, RooCode, Amazon Q Developer, Goose
    - OpenCode (prints stdio config + link to vendor docs)
    
    Uses the embedded LadybugDB database (no database setup needed).
    """
    console.print("\n[bold cyan]MCP Client Setup[/bold cyan]")
    console.print("Configure your IDE or CLI tool to use Code Visualizer.\n")
    configure_mcp_client()

@mcp_app.command("start")
def mcp_start():
    """
    Start the Code Visualizer MCP server.
    
    Starts the server which listens for JSON-RPC requests from stdin.
    This is used by IDE integrations (VS Code, Cursor, Zed, etc.).
    """
    console.print("[bold green]Starting Code Visualizer Server...[/bold green]")
    _load_credentials()

    server = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        server = MCPServer(loop=loop, cwd=Path.cwd())
        loop.run_until_complete(server.run())
    except ValueError as e:
        # This typically happens if credentials are still not found after all checks.
        console.print(f"[bold red]Configuration Error:[/bold red] {e}")
        console.print("Please check the LadybugDB configuration (`cviz doctor`).")
        raise typer.Exit(code=1) from e
    except KeyboardInterrupt:
        # Handle graceful shutdown on Ctrl+C.
        console.print("\n[bold yellow]Server stopped by user.[/bold yellow]")
    finally:
        # Ensure server and event loop are properly closed.
        if server:
            server.shutdown()
        loop.close()

@mcp_app.command("tools")
def mcp_tools():
    """
    List all available MCP tools.
    
    Shows all tools that can be called by AI assistants through the MCP interface.
    """
    _load_credentials()
    console.print("[bold green]Available MCP Tools:[/bold green]")
    try:
        # Instantiate the server to access the tool definitions.
        server = MCPServer()
        tools = server.tools.values()

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Tool Name", style="dim", width=30)
        table.add_column("Description")

        for tool in sorted(tools, key=lambda t: t['name']):
            table.add_row(tool['name'], tool['description'])

        console.print(table)

    except ValueError as e:
        console.print(f"[bold red]Error loading tools:[/bold red] {e}")
        console.print("Please ensure your database is configured correctly.")
        raise typer.Exit(code=1) from e
    except Exception as e:
        console.print(f"[bold red]An unexpected error occurred:[/bold red] {e}")
        raise typer.Exit(code=1) from e

# Abbreviation for mcp setup
@app.command("m", rich_help_panel="Shortcuts")
def mcp_setup_alias():
    """Shortcut for 'cviz mcp setup'"""
    mcp_setup()


# Create Context command group
def _load_credentials(cli_context_flag: Optional[str] = None):
    """
    Loads configuration and credentials from various sources into environment variables.
    Uses per-variable precedence - each variable is loaded from the highest priority source.
    Priority order (highest to lowest):
    1. Runtime environment variables (shell/CI)
    2. Local `.code-visualizer/.env` and `.env` in the current project directory (per-repo mode only)
    3. Global `~/.code-visualizer/.env` (user defaults, including `cviz config set`)
    4. Local `mcp.json` env vars (project defaults)

    Duplicate loading is skipped when the local file resolves to the same path as the global file.
    Arbitrary parent directory `.env` files are not loaded—ensuring isolation.
    """
    from dotenv import dotenv_values
    from code_visualizer.cli.config_manager import (
        ensure_config_dir,
        code_visualizer_dotenv_at_cwd,
        normalize_config_path,
    )
    
    # Ensure config directory exists (lazy initialization)
    ensure_config_dir()
    
    # Snapshot runtime environment BEFORE merging config files.
    # These values must remain highest priority.
    runtime_env = dict(os.environ)

    # Collect all config sources in precedence order (lowest to highest)
    config_sources = []
    config_source_names = []
    key_source_map = {}
    key_defined_in = {}

    def _append_source(source_name: str, source_values: dict):
        if not source_values:
            return
        config_sources.append(source_values)
        config_source_names.append(source_name)
        for k, v in source_values.items():
            if v is None:
                continue
            key_source_map[k] = source_name
            key_defined_in.setdefault(k, []).append(source_name)

    # 4. Local mcp.json (lowest priority - project defaults)
    mcp_file_path = Path.cwd() / "mcp.json"
    if mcp_file_path.exists():
        try:
            with open(mcp_file_path, "r", encoding="utf-8", errors="replace") as f:
                mcp_config = json.load(f)
            from .config_manager import MCP_SERVER_KEY

            server_env = mcp_config.get("mcpServers", {}).get(MCP_SERVER_KEY, {}).get("env", {})
            if isinstance(server_env, dict):
                normalized_env = {}
                for env_key, env_value in server_env.items():
                    if env_value is not None and "PATH" in env_key:
                        normalized_env[env_key] = normalize_config_path(str(env_value), absolute=True)
                    else:
                        normalized_env[env_key] = env_value
                server_env = normalized_env
            _append_source("mcp.json", server_env)
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load mcp.json: {e}[/yellow]")
    
    # 3. Global .env file (user defaults)
    global_env_path = Path.home() / ".code-visualizer" / ".env"
    if global_env_path.exists():
        try:
            with open(global_env_path, "r", encoding="utf-8", errors="replace") as f:
                _append_source(str(global_env_path), dotenv_values(stream=f))
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load global .env: {e}[/yellow]")
    
    # 2. Local project .env (only when cwd is under HOME, or CVIZ_LOAD_PROJECT_ENV=1)
    if config_manager.should_apply_project_dotenv():
        try:
            local_dot_env = Path.cwd() / ".env"
            if local_dot_env.exists() and local_dot_env.resolve() != global_env_path.resolve():
                with open(local_dot_env, "r", encoding="utf-8", errors="replace") as f:
                    _append_source(str(local_dot_env), dotenv_values(stream=f))
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load .env from current directory: {e}[/yellow]")

        # <cwd>/.code-visualizer/.env only (overrides global when distinct)
        try:
            local_cviz_env = code_visualizer_dotenv_at_cwd(Path.cwd())
            if local_cviz_env and local_cviz_env.resolve() != global_env_path.resolve():
                with open(local_cviz_env, "r", encoding="utf-8", errors="replace") as f:
                    vals = dotenv_values(stream=f)
                    _append_source(str(local_cviz_env), vals)
        except Exception as e:
            console.print(
                f"[yellow]Warning: Could not load .code-visualizer/.env at cwd: {e}[/yellow]"
            )
    
    # Merge all configs with proper precedence (later sources override earlier ones)
    merged_config = {}
    for config in config_sources:
        merged_config.update(config)
    
    # Apply merged config to environment, but never override runtime env.
    # Apply merged config to environment.
    # IMPORTANT: DB-selection keys set in the shell must win over .env defaults.
    # E.g. a DEFAULT_DATABASE given on the command line must not be overridden
    # by the one sitting in ~/.code-visualizer/.env
    for key, value in merged_config.items():
        if value is not None:  # Only set non-None values
            if key in runtime_env:
                continue
            if "PATH" in key:
                value = normalize_config_path(str(value), absolute=True)
            os.environ[key] = str(value)
    
    # Report what was loaded
    if config_source_names:
        if len(config_source_names) == 1:
            console.print(f"[dim]Loaded configuration from: {config_source_names[-1]}[/dim]")
        else:
            console.print(f"[dim]Loaded configuration from: {', '.join(config_source_names)}[/dim]")
    else:
        console.print("[yellow]No configuration file found. Using defaults.[/yellow]")

    default_db_sources = list(key_defined_in.get("DEFAULT_DATABASE", []))
    if "DEFAULT_DATABASE" in runtime_env:
        default_db_sources.append("environment")

    if len(default_db_sources) > 1:
        winners = "environment" if "DEFAULT_DATABASE" in runtime_env else key_source_map.get("DEFAULT_DATABASE", "defaults")
        console.print(
            "[dim]DEFAULT_DATABASE defined in multiple sources: "
            f"{', '.join(default_db_sources)}; using: {winners}[/dim]"
        )
    
    
    # Determine if there is a runtime database override.
    runtime_db = os.environ.get("CVIZ_RUNTIME_DB_TYPE")

    # Now select the database based on precedence:
    # 1. CVIZ_RUNTIME_DB_TYPE
    # 2. DATABASE_TYPE or DEFAULT_DATABASE from runtime environment (shell variables)
    # 3. DATABASE_TYPE or DEFAULT_DATABASE from merged config files (.env files)
    # 4. Default (ladybugdb)
    if runtime_db:
        default_db = runtime_db.lower()
        db_source = "runtime-env (CVIZ_RUNTIME_DB_TYPE)"
    elif "DATABASE_TYPE" in runtime_env:
        default_db = runtime_env["DATABASE_TYPE"].lower()
        db_source = "environment (DATABASE_TYPE)"
    elif "DEFAULT_DATABASE" in runtime_env:
        default_db = runtime_env["DEFAULT_DATABASE"].lower()
        db_source = "environment (DEFAULT_DATABASE)"
    elif os.environ.get("DATABASE_TYPE") and "DATABASE_TYPE" in key_source_map:
        default_db = os.environ["DATABASE_TYPE"].lower()
        db_source = key_source_map["DATABASE_TYPE"]
    elif os.environ.get("DEFAULT_DATABASE") and "DEFAULT_DATABASE" in key_source_map:
        default_db = os.environ["DEFAULT_DATABASE"].lower()
        db_source = key_source_map["DEFAULT_DATABASE"]
    else:
        default_db = "ladybugdb"
        db_source = "default"

    # Print selection banner
    if default_db == "ladybugdb":
        console.print(f"[cyan]Using database: ladybugdb (source: {db_source})[/cyan]")
    else:
        console.print(
            f"[yellow]⚠ Database '{default_db}' (source: {db_source}) is not supported: "
            "Code Visualizer only uses LadybugDB.[/yellow]"
        )

    # Persist selection metadata for downstream diagnostics and error messages.
    os.environ["CVIZ_SELECTED_DATABASE"] = default_db
    os.environ["CVIZ_DB_SELECTION_SOURCE"] = db_source



# ============================================================================
# CONFIG COMMAND GROUP
# ============================================================================

config_app = typer.Typer(help="Manage configuration settings")
app.add_typer(config_app, name="config")

@config_app.command("show")
def config_show():
    """
    Display current configuration settings.
    
    Shows all configuration values including database, indexing options,
    logging settings, and performance tuning parameters.
    """
    config_manager.show_config()

@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key to set"),
    value: str = typer.Argument(..., help="Value to set")
):
    """
    Set a configuration value.
    
    Examples:
        cviz config set SEMANTIC_SEARCH true
        cviz config set INDEX_VARIABLES false
        cviz config set MAX_FILE_SIZE_MB 20
        cviz config set DEBUG_LOGS true
    """
    if not config_manager.set_config_value(key, value):
        raise typer.Exit(code=1)

@config_app.command("reset")
def config_reset():
    """
    Reset all configuration to default values.
    
    This will restore all settings to their defaults.
    Your current configuration will be backed up.
    """
    if typer.confirm("Are you sure you want to reset all configuration to defaults?", default=False):
        config_manager.reset_config()
    else:
        console.print("[yellow]Reset cancelled[/yellow]")

@config_app.command("db")
def config_db(backend: str = typer.Argument(..., help="Database backend: only 'ladybugdb' is supported")):
    """
    Set the default database backend (kept for compatibility: LadybugDB is the
    only backend of Code Visualizer).

    Example:
        cviz config db ladybugdb
    """
    backend = backend.lower()
    if backend != "ladybugdb":
        console.print(f"[bold red]Unsupported backend: {backend}[/bold red]")
        console.print("Code Visualizer only uses LadybugDB ('ladybugdb').")
        raise typer.Exit(code=1)

    updated = config_manager.set_config_value("DEFAULT_DATABASE", backend)
    if not updated:
        console.print(f"[bold red]Failed to switch default database to {backend}[/bold red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✔ Default database switched to {backend}[/green]")

# ============================================================================
# PROMPT COMMAND GROUP - Custom LLM Prompts
# ============================================================================

prompt_app = typer.Typer(help="Manage custom LLM prompt files")
app.add_typer(prompt_app, name="prompt")

@prompt_app.command("add")
def prompt_add(
    path: str = typer.Argument(..., help="Path to the prompt file to register")
):
    """
    Add a custom prompt file to the project.
    
    Registers a prompt file that will be injected into the LLM system prompt.
    The file path is stored relative to the project root.
    
    Examples:
        cviz prompt add skills.md
        cviz prompt add docs/custom-instructions.txt
        cviz prompt add /absolute/path/to/prompt.md
    """
    try:
        success = project_config.add_prompt_file(path)
        if not success:
            raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red]Error adding prompt file: {e}[/red]")
        raise typer.Exit(code=1)

@prompt_app.command("list")
def prompt_list():
    """
    List all registered prompt files.
    
    Shows all custom prompt files that will be injected into the LLM system prompt.
    Files are shown in the order they will be prepended.
    """
    try:
        prompts = project_config.list_prompt_files()
        
        if not prompts:
            console.print("[yellow]No custom prompt files registered.[/yellow]")
            console.print("\nUse [cyan]cviz prompt add <path>[/cyan] to register a prompt file.")
            return
        
        console.print("[bold cyan]Registered Prompt Files:[/bold cyan]\n")
        
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=3)
        table.add_column("Path", style="green")
        table.add_column("Status", style="cyan", width=10)
        
        project_root = project_config.get_project_root()
        for i, prompt_path in enumerate(prompts, 1):
            # Check if file exists
            prompt_file = Path(prompt_path)
            if not prompt_file.is_absolute():
                prompt_file = project_root / prompt_file
            
            status = "✅ Found" if prompt_file.exists() else "⚠️ Missing"
            table.add_row(str(i), prompt_path, status)
        
        console.print(table)
        
        config_file = project_config.get_project_config_file()
        console.print(f"\n[dim]Config: {config_file}[/dim]")
        
    except Exception as e:
        console.print(f"[red]Error listing prompt files: {e}[/red]")
        raise typer.Exit(code=1)

@prompt_app.command("remove")
def prompt_remove(
    path: str = typer.Argument(..., help="Path to the prompt file to unregister")
):
    """
    Remove a custom prompt file from the project.
    
    Unregisters a prompt file so it will no longer be injected into the LLM system prompt.
    
    Examples:
        cviz prompt remove skills.md
        cviz prompt remove docs/custom-instructions.txt
    """
    try:
        success = project_config.remove_prompt_file(path)
        if not success:
            raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red]Error removing prompt file: {e}[/red]")
        raise typer.Exit(code=1)

# ============================================================================
# BUNDLE COMMAND GROUP - Pre-indexed Graph Snapshots
# ============================================================================

bundle_app = typer.Typer(help="Create and load pre-indexed graph bundles")
app.add_typer(bundle_app, name="bundle")

@bundle_app.command("export")
def bundle_export(
    output: str = typer.Argument(..., help="Output path for the .cviz bundle file"),
    repo: Optional[str] = typer.Option(None, "--repo", "-r", help="Specific repository path to export (default: export all)"),
    no_stats: bool = typer.Option(False, "--no-stats", help="Skip statistics generation"),
    sign_key: Optional[str] = typer.Option(None, "--sign-key", envvar="CVIZ_BUNDLE_SIGN_KEY", help="HMAC signing key (or set CVIZ_BUNDLE_SIGN_KEY)"),
    encrypt_password: Optional[str] = typer.Option(None, "--encrypt-password", envvar="CVIZ_BUNDLE_PASSWORD", help="Encrypt bundle with this password (or set CVIZ_BUNDLE_PASSWORD)", hide_input=True),
    exclude_labels: Optional[str] = typer.Option(None, "--exclude-labels", help="Comma-separated node labels to leave out of the bundle, with their edges (e.g. DbTable,ExternalClass) (#1323)"),
):
    """
    Export the current graph to a portable .cviz bundle.
    
    Creates a pre-indexed graph snapshot that can be distributed and loaded
    instantly without re-indexing. Perfect for sharing famous repositories.
    
    Examples:
        cviz bundle export numpy.cviz --repo /path/to/numpy
        cviz bundle export my-project.cviz
        cviz bundle export all-repos.cviz --no-stats
    """
    _load_credentials()
    from code_visualizer.core.cviz_bundle import CvizBundle

    # Typer resolves defaults only when *it* invokes the command. Called as a
    # plain function (tests/integration/test_parser_goldens.py does this, as can
    # any programmatic caller), an omitted option keeps its OptionInfo sentinel
    # -- which is truthy, so `if sign_key:` passes and `sign_key.encode()` then
    # raises AttributeError. These two are normalized rather than merely
    # forwarded because they are the only options here that get dereferenced.
    if not isinstance(sign_key, (str, type(None))):
        sign_key = None
    if not isinstance(encrypt_password, (str, type(None))):
        encrypt_password = None

    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, _, code_finder = services[:3]

    try:
        output_path = Path(output)
        repo_path = Path(repo).resolve() if repo else None
        
        console.print(f"[cyan]Exporting graph to {output_path}...[/cyan]")
        if repo_path:
            console.print(f"[dim]Repository: {repo_path}[/dim]")
        else:
            console.print("[dim]Exporting all repositories[/dim]")
        
        bundle = CvizBundle(db_manager)
        success, message = bundle.export_to_bundle(
            output_path,
            repo_path=repo_path,
            include_stats=not no_stats,
            sign_key=sign_key,
            encrypt_password=encrypt_password,
            # bundle_export is also called programmatically (the golden
            # harness does), where unpassed Typer options arrive as OptionInfo
            # objects rather than their defaults — same trap the CLI meta-test
            # documents for other commands.
            exclude_labels=[
                l for l in (exclude_labels if isinstance(exclude_labels, str) else "").split(",")
                if l.strip()
            ],
        )
        
        if success:
            console.print(f"[bold green]{message}[/bold green]")
        else:
            console.print(f"[bold red]Export failed: {message}[/bold red]")
            raise typer.Exit(code=1)
    
    finally:
        db_manager.close_driver()

def _confirm_bundle_clear(clear: bool, yes: bool) -> bool:
    """Return True if import may proceed; False if user cancelled."""
    if not clear:
        return True
    console.print("[yellow]⚠️  Warning: This will clear all existing graph data![/yellow]")
    if yes:
        return True
    if not sys.stdin.isatty():
        console.print(
            "[bold red]Refusing to clear graph in non-interactive mode. "
            "Pass --yes / -y to confirm.[/bold red]"
        )
        raise typer.Exit(code=1)
    if not typer.confirm("Are you sure you want to continue?", default=False):
        console.print("[yellow]Import cancelled[/yellow]")
        return False
    return True


@bundle_app.command("import")
def bundle_import(
    bundle_file: str = typer.Argument(..., help="Path to the .cviz bundle file to import"),
    clear: bool = typer.Option(False, "--clear", help="Clear existing graph data before importing"),
    password: Optional[str] = typer.Option(None, "--password", envvar="CVIZ_BUNDLE_PASSWORD", help="Password for encrypted bundles (or set CVIZ_BUNDLE_PASSWORD)", hide_input=True),
    verify_key: Optional[str] = typer.Option(None, "--verify-key", envvar="CVIZ_BUNDLE_VERIFY_KEY", help="HMAC verification key for signed bundles (or set CVIZ_BUNDLE_VERIFY_KEY)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation when using --clear"),
):
    """
    Import a .cviz bundle into the current database.
    
    Loads a pre-indexed graph snapshot into your database. Use --clear to
    replace all existing data with the bundle contents.
    
    Examples:
        cviz bundle import numpy.cviz
        cviz bundle import my-project.cviz --clear --yes
    """
    _load_credentials()
    from code_visualizer.core.cviz_bundle import CvizBundle
    
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        bundle_path = Path(bundle_file)
        
        if not bundle_path.exists():
            console.print(f"[bold red]Bundle file not found: {bundle_path}[/bold red]")
            raise typer.Exit(code=1)
        
        if not _confirm_bundle_clear(clear, yes):
            return
        
        console.print(f"[cyan]Importing bundle from {bundle_path}...[/cyan]")
        
        bundle = CvizBundle(db_manager)
        success, message = bundle.import_from_bundle(
            bundle_path,
            clear_existing=clear,
            password=password,
            verify_key=verify_key,
        )
        
        if success:
            console.print(f"[bold green]{message}[/bold green]")
        else:
            console.print(f"[bold red]Import failed: {message}[/bold red]")
            raise typer.Exit(code=1)
    
    finally:
        db_manager.close_driver()

@bundle_app.command("verify")
def bundle_verify(
    bundle_file: str = typer.Argument(..., help="Path to the .cviz bundle file to verify"),
    password: Optional[str] = typer.Option(None, "--password", envvar="CVIZ_BUNDLE_PASSWORD", help="Password for encrypted bundles (or set CVIZ_BUNDLE_PASSWORD)", hide_input=True),
    verify_key: Optional[str] = typer.Option(None, "--verify-key", envvar="CVIZ_BUNDLE_VERIFY_KEY", help="HMAC verification key for signed bundles (or set CVIZ_BUNDLE_VERIFY_KEY)"),
):
    """Verify bundle structure, checksums, optional signature, and encryption envelope."""
    from code_visualizer.core.cviz_bundle import CvizBundle

    bundle = CvizBundle(None)
    success, message = bundle.verify_bundle(Path(bundle_file), password=password, verify_key=verify_key)
    if success:
        console.print(f"[bold green]Bundle verified:[/bold green] {message}")
    else:
        console.print(f"[bold red]Bundle verification failed:[/bold red] {message}")
        raise typer.Exit(code=1)


@bundle_app.command("inspect")
def bundle_inspect(
    bundle_file: str = typer.Argument(..., help="Path to the .cviz bundle file to inspect"),
    password: Optional[str] = typer.Option(None, "--password", envvar="CVIZ_BUNDLE_PASSWORD", help="Password for encrypted bundles (or set CVIZ_BUNDLE_PASSWORD)", hide_input=True),
    verify_key: Optional[str] = typer.Option(None, "--verify-key", envvar="CVIZ_BUNDLE_VERIFY_KEY", help="HMAC verification key for signed bundles (or set CVIZ_BUNDLE_VERIFY_KEY)"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON inspection output"),
):
    """Inspect bundle metadata without importing it."""
    from code_visualizer.core.cviz_bundle import CvizBundle

    bundle = CvizBundle(None)
    success, info = bundle.inspect_bundle(Path(bundle_file), password=password, verify_key=verify_key)
    if not success:
        console.print(f"[bold red]Inspect failed:[/bold red] {info.get('error', 'unknown error')}")
        raise typer.Exit(code=1)
    if json_output:
        console.print_json(data=info)
        return

    metadata = info.get("metadata", {})
    stats = info.get("stats", {})
    graph_metrics = metadata.get("graph_metrics", {})
    table = Table(title=f"Bundle: {Path(bundle_file).name}", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value", overflow="fold")
    table.add_row("Repository", str(metadata.get("repo", "unknown")))
    table.add_row("Name", str(metadata.get("name", Path(bundle_file).name)))
    table.add_row("Format", str(metadata.get("format_version", metadata.get("cviz_version", "unknown"))))
    table.add_row("Exported", str(metadata.get("exported_at", "unknown")))
    table.add_row("Nodes", str(graph_metrics.get("total_nodes", stats.get("node_count", "unknown"))))
    table.add_row("Edges", str(graph_metrics.get("total_edges", stats.get("edge_count", "unknown"))))
    table.add_row("Encrypted", "yes" if info.get("encrypted") else "no")
    table.add_row("Signed", "yes" if info.get("signed") else "no")
    table.add_row("Valid", "yes" if info.get("valid") else f"no ({info.get('validation')})")
    console.print(table)


@bundle_app.command("diff")
def bundle_diff(
    left_bundle: str = typer.Argument(..., help="Older/base .cviz bundle"),
    right_bundle: str = typer.Argument(..., help="Newer/target .cviz bundle"),
    left_password: Optional[str] = typer.Option(None, "--left-password", help="Password for the left encrypted bundle", hide_input=True),
    right_password: Optional[str] = typer.Option(None, "--right-password", help="Password for the right encrypted bundle", hide_input=True),
    left_verify_key: Optional[str] = typer.Option(None, "--left-verify-key", help="HMAC verification key for the left signed bundle"),
    right_verify_key: Optional[str] = typer.Option(None, "--right-verify-key", help="HMAC verification key for the right signed bundle"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON diff output"),
    limit: int = typer.Option(20, "--limit", help="Maximum changed keys to show per category"),
):
    """Compare two .cviz bundles without importing them."""
    from code_visualizer.core.cviz_bundle import CvizBundle

    bundle = CvizBundle(None)
    success, diff = bundle.diff_bundles(
        Path(left_bundle),
        Path(right_bundle),
        left_password=left_password or os.environ.get("CVIZ_BUNDLE_PASSWORD"),
        right_password=right_password or os.environ.get("CVIZ_BUNDLE_PASSWORD"),
        left_verify_key=left_verify_key or os.environ.get("CVIZ_BUNDLE_VERIFY_KEY"),
        right_verify_key=right_verify_key or os.environ.get("CVIZ_BUNDLE_VERIFY_KEY"),
    )
    if not success:
        console.print(f"[bold red]Diff failed:[/bold red] {diff.get('error', 'unknown error')}")
        raise typer.Exit(code=1)
    if json_output:
        console.print_json(data=diff)
        return

    table = Table(title="Bundle Diff")
    table.add_column("Category", style="cyan")
    table.add_column("Added", justify="right", style="green")
    table.add_column("Removed", justify="right", style="red")
    table.add_column("Changed", justify="right", style="yellow")
    for category in ("nodes", "edges"):
        item = diff[category]
        table.add_row(category, str(len(item["added"])), str(len(item["removed"])), str(len(item["changed"])))
    console.print(table)

    for category in ("nodes", "edges"):
        item = diff[category]
        samples = []
        for label in ("added", "removed", "changed"):
            values = item[label][:limit]
            if values:
                samples.append(f"[bold]{category} {label}[/bold]\n" + "\n".join(f"  {value}" for value in values))
        if samples:
            console.print("\n".join(samples))

@bundle_app.command("load")
def bundle_load(
    bundle_name: str = typer.Argument(..., help="Bundle name or path to load (e.g., 'numpy' or 'numpy.cviz')"),
    clear: bool = typer.Option(False, "--clear", help="Clear existing graph data before loading"),
    password: Optional[str] = typer.Option(None, "--password", envvar="CVIZ_BUNDLE_PASSWORD", help="Password for encrypted bundles (or set CVIZ_BUNDLE_PASSWORD)", hide_input=True),
    verify_key: Optional[str] = typer.Option(None, "--verify-key", envvar="CVIZ_BUNDLE_VERIFY_KEY", help="HMAC verification key for signed bundles (or set CVIZ_BUNDLE_VERIFY_KEY)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation when using --clear"),
):
    """
    Load a pre-indexed bundle file into the database.
    
    Examples:
        cviz load numpy
        cviz load numpy.cviz --clear
        cviz load /path/to/bundle.cviz
    """
    _load_credentials()
    
    bundle_path = Path(bundle_name)
    
    # If it's an absolute path or has .cviz extension and exists, use it directly
    if bundle_path.is_absolute() or (bundle_path.suffix == '.cviz' and bundle_path.exists()):
        bundle_import(str(bundle_path), clear=clear, password=password, verify_key=verify_key, yes=yes)
        return
    
    # Add .cviz extension if not present
    if not bundle_path.suffix:
        bundle_path = Path(f"{bundle_name}.cviz")
    
    # Check if exists locally
    if bundle_path.exists():
        console.print(f"[dim]Found local bundle: {bundle_path}[/dim]")
        bundle_import(str(bundle_path), clear=clear, password=password, verify_key=verify_key, yes=yes)
        return
    
    console.print(f"[bold red]No bundle at {bundle_path}[/bold red]")
    raise typer.Exit(code=1)

@bundle_app.command("merge")
def bundle_merge(
    ancestor: str = typer.Argument(..., help="Common ancestor version of the bundle (%O)"),
    current: str = typer.Argument(..., help="Current branch version of the bundle (%A); also the merge result"),
    other: str = typer.Argument(..., help="Other branch version of the bundle (%B)"),
):
    """
    Git merge driver for .cviz bundle files.

    Registered by `cviz hook install` as `merge.cviz-bundle.driver = cviz bundle
    merge %O %A %B`. Git invokes it with temp files holding the ancestor (%O),
    current (%A) and other (%B) versions; the merge result must be left in the
    %A file and the driver must exit 0 for the merge to proceed.

    Bundles are binary snapshots that cannot be merged line-by-line, so the
    strategy is: if both sides are identical, accept either; otherwise keep
    the current branch's version (already in the %A file) and warn that the
    bundle should be regenerated with `cviz export` after the merge.
    """
    current_path = Path(current)
    other_path = Path(other)

    def _read_bytes(p: Path) -> bytes:
        # A missing or empty temp file means the side has no content
        # (e.g. the bundle is a new file on both branches → empty ancestor).
        try:
            return p.read_bytes() if p.exists() else b""
        except OSError:
            return b""

    current_bytes = _read_bytes(current_path)
    other_bytes = _read_bytes(other_path)

    if current_bytes == other_bytes:
        # Both branches have identical bundle contents; nothing to do.
        raise typer.Exit(code=0)

    # Keep the current branch's version (the %A file already contains it).
    console.print(
        "[yellow]⚠ Conflicting changes to a .cviz bundle were detected during merge. "
        "Keeping the current branch's version.[/yellow]"
    )
    console.print(
        "[yellow]The bundle may be stale — regenerate it with 'cviz export' after the merge.[/yellow]"
    )
    raise typer.Exit(code=0)

# ============================================================================
# HOOK COMMAND GROUP - Git integration
# ============================================================================

hook_app = typer.Typer(help="Install Git hooks that keep the CViz graph in sync")
app.add_typer(hook_app, name="hook")


@hook_app.command("install")
def hook_install(
    path: str = typer.Argument(".", help="Path inside the Git repository"),
    force: bool = typer.Option(False, "--force", "-f", help="Replace existing non-CViz hook files"),
):
    """
    Install CViz-managed Git hooks in the nearest repository.

    The installed hooks (post-checkout, post-merge, post-rewrite) run
    `cviz update <repo> --quiet`, which re-processes only the files whose
    content changed. Existing non-CViz hooks are preserved unless --force is used.
    """
    try:
        status = install_hooks(path, force=force)
    except HookError as exc:
        console.print(f"[bold red]Hook install failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]✓[/green] Installed CViz hooks in [bold]{status.repo_root}[/bold]")
    console.print(f"[dim]Git directory: {status.git_dir}[/dim]")


@hook_app.command("uninstall")
def hook_uninstall(
    path: str = typer.Argument(".", help="Path inside the Git repository"),
):
    """Remove CViz-managed Git hooks and local merge-driver config."""
    try:
        status = uninstall_hooks(path)
    except HookError as exc:
        console.print(f"[bold red]Hook uninstall failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]✓[/green] Removed CViz hooks from [bold]{status.repo_root}[/bold]")


@hook_app.command("status")
def hook_status(
    path: str = typer.Argument(".", help="Path inside the Git repository"),
):
    """Show whether CViz-managed Git hooks are installed."""
    try:
        status = get_hook_status(path)
    except HookError as exc:
        console.print(f"[bold red]Hook status failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    table = Table(title="CViz Git Hook Status", show_header=True, header_style="bold magenta")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_row("Repository", str(status.repo_root))
    table.add_row("Git directory", str(status.git_dir))
    table.add_row("Managed hooks", ", ".join(status.installed_hooks) or "none")
    table.add_row("Unmanaged hooks", ", ".join(status.unmanaged_hooks) or "none")
    table.add_row("Merge driver", "installed" if status.has_merge_driver else "missing")
    table.add_row(".gitattributes", "installed" if status.has_gitattributes_entry else "missing")
    console.print(table)

# Shortcut commands at root level
@app.command("export", rich_help_panel="Bundle Shortcuts")
def export_shortcut(
    output: str = typer.Argument(..., help="Output path for the .cviz bundle file"),
    repo: Optional[str] = typer.Option(None, "--repo", "-r", help="Specific repository path to export"),
    no_stats: bool = typer.Option(False, "--no-stats", help="Skip generating statistics in the bundle"),
    sign_key: Optional[str] = typer.Option(None, "--sign-key", envvar="CVIZ_BUNDLE_SIGN_KEY", help="HMAC signing key (or set CVIZ_BUNDLE_SIGN_KEY)"),
    encrypt_password: Optional[str] = typer.Option(None, "--encrypt-password", envvar="CVIZ_BUNDLE_PASSWORD", help="Encrypt bundle with this password (or set CVIZ_BUNDLE_PASSWORD)", hide_input=True),
    exclude_labels: Optional[str] = typer.Option(None, "--exclude-labels", help="Comma-separated node labels to leave out of the bundle, with their edges (e.g. DbTable,ExternalClass) (#1323)"),
):
    """Shortcut for 'cviz bundle export'"""
    bundle_export(
        output=output,
        repo=repo,
        no_stats=no_stats,
        sign_key=sign_key,
        encrypt_password=encrypt_password,
        exclude_labels=exclude_labels,
    )

@app.command("diagram")
def diagram(
    path: Optional[str] = typer.Argument(None, help="Repository path to scope the diagram to"),
    output_format: str = typer.Option("mermaid", "--format", help="Diagram format: mermaid or dot (#1287)"),
    level: str = typer.Option("file", "--level", help="Granularity: 'file' (file→module imports) or 'call' (function call graph)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write to a file instead of stdout"),
    limit: int = typer.Option(300, "--limit", "-l", help="Maximum edges to include (truncation is reported in the diagram)"),
):
    """
    Export the context graph as a Mermaid or Graphviz DOT diagram.

    Mermaid output pastes straight into GitHub Markdown / Notion; DOT renders
    locally with graphviz. The diagram is a generated artifact of the current
    index — regenerate it after refactors rather than committing it as
    documentation.

    Example:
        cviz diagram . > architecture.mmd
        cviz diagram . --format dot -o graph.dot && dot -Tsvg graph.dot -o graph.svg
        cviz diagram . --level call --limit 100
    """
    output_format = output_format.lower()
    if output_format not in ("mermaid", "dot"):
        console.print(f"[red]Unknown --format '{output_format}' (expected mermaid or dot)[/red]")
        raise typer.Exit(code=2)
    if level not in ("file", "call"):
        console.print(f"[red]Unknown --level '{level}' (expected file or call)[/red]")
        raise typer.Exit(code=2)
    # stdout carries only the diagram; init chatter goes to stderr for the
    # init window only (restored after — a permanent rebind leaks a closed
    # stream into later in-process invocations).
    import sys as _sys
    from . import cli_helpers as _cli_helpers
    # NB: the .file property GETTER materializes the current (possibly
    # transient, CliRunner-captured) stream; the dynamic state lives in
    # ._file, which is None while the console follows sys.stdout live.
    _prev_console_file = _cli_helpers.console._file
    if output is None:
        _cli_helpers.console.file = _sys.stderr
    try:
        _load_credentials()
        services = _initialize_services()
    finally:
        _cli_helpers.console._file = _prev_console_file
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]

    try:
        repo_path = Path(path).resolve().as_posix() if path else None
        result = code_finder.export_diagram_edges(level=level, repo_path=repo_path, limit=limit)
    finally:
        db_manager.close_driver()

    edges = result["edges"]
    ids: dict = {}

    def _node_id(name: str) -> str:
        if name not in ids:
            ids[name] = f"n{len(ids)}"
        return ids[name]

    lines = []
    if output_format == "mermaid":
        lines.append("graph LR")
        if result["truncated"]:
            lines.append(f"    %% truncated: showing {len(edges)} of {result['total_count']} edges (raise --limit)")
        for src, dst in edges:
            sid, did = _node_id(src), _node_id(dst)
            s_label = src.replace('"', "'")
            d_label = dst.replace('"', "'")
            lines.append(f'    {sid}["{s_label}"] --> {did}["{d_label}"]')
        if not edges:
            lines.append("    %% no edges found for this scope")
    else:
        lines.append("digraph cviz {")
        lines.append("    rankdir=LR;")
        if result["truncated"]:
            lines.append(f"    // truncated: showing {len(edges)} of {result['total_count']} edges (raise --limit)")
        for src, dst in edges:
            sid, did = _node_id(src), _node_id(dst)
            lines.append(f'    {sid} -> {did};')
        for name, nid in ids.items():
            label = name.replace('"', "'")
            lines.append(f'    {nid} [label="{label}"];')
        lines.append("}")

    text = "\n".join(lines) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
        console.print(f"[green]✅ Wrote {output_format} diagram ({len(edges)} edges) to {output}[/green]")
        if result["truncated"]:
            console.print(f"[yellow]⚠ Truncated: {result['total_count']} total edges; raise --limit to include all[/yellow]")
    else:
        print(text, end="")


@app.command("load", rich_help_panel="Bundle Shortcuts")
def load_shortcut(
    bundle_name: str = typer.Argument(..., help="Bundle name or path to load"),
    clear: bool = typer.Option(False, "--clear", help="Clear existing graph data before loading"),
    password: Optional[str] = typer.Option(None, "--password", envvar="CVIZ_BUNDLE_PASSWORD", help="Password for encrypted bundles (or set CVIZ_BUNDLE_PASSWORD)", hide_input=True),
    verify_key: Optional[str] = typer.Option(None, "--verify-key", envvar="CVIZ_BUNDLE_VERIFY_KEY", help="HMAC verification key for signed bundles (or set CVIZ_BUNDLE_VERIFY_KEY)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation when using --clear"),
):
    """Shortcut for 'cviz bundle load'"""
    # Must pass every option explicitly: invoked as a plain function, Typer does not
    # resolve defaults, so an omitted parameter keeps its OptionInfo sentinel.
    bundle_load(bundle_name, clear, password=password, verify_key=verify_key, yes=yes)

# Create API command group
api_app = typer.Typer(help="CViz Gateway (HTTP API) commands")
app.add_typer(api_app, name="api")

@api_app.command("start")
def api_start(
    host: str = typer.Option("0.0.0.0", help="Host to bind the server to"),
    port: int = typer.Option(8000, help="Port to bind the server to"),
    reload: bool = typer.Option(False, help="Enable auto-reload (development only)"),
):
    """
    Start the CViz Gateway HTTP API server.
    
    This server provides a REST API that can be used by ChatGPT Actions,
    Claude, or web frontends to interact with the Code Visualizer graph.
    """
    import uvicorn
    console.print(f"[bold green]Starting CViz Gateway on {host}:{port}...[/bold green]")
    _load_credentials()
    uvicorn.run("code_visualizer.api.app:app", host=host, port=port, reload=reload)



# ============================================================================
# DOCTOR DIAGNOSTIC COMMAND
# ============================================================================

@app.command()
def doctor():
    """
    Run diagnostics to check system health and configuration.
    
    Checks:
    - Configuration validity
    - Database connectivity
    - Tree-sitter installation
    - Required dependencies
    - File permissions
    """
    console.print("[bold cyan]🏥 Running Code Visualizer Diagnostics...[/bold cyan]\n")
    
    all_checks_passed = True
    # Warnings are not failures, but printing "All diagnostics passed! System is
    # healthy." while a ⚠ is on screen is misleading — `doctor` is the command
    # people run when something is already wrong.
    warnings_found = False

    config_manager.ensure_first_run_bootstrap()
    config_manager.ensure_config_file()
    
    # 1. Check configuration
    console.print("[bold]1. Checking Configuration...[/bold]")
    config = {}
    try:
        config = config_manager.load_config()
        
        if config_manager.CONFIG_FILE.exists():
            console.print(f"   [green]✓[/green] Config loaded from {config_manager.CONFIG_FILE}")
        else:
            console.print("   [yellow]ℹ[/yellow] No .env config found, using defaults")
            console.print(f"   [dim]Config will be created at: {config_manager.CONFIG_FILE}[/dim]")
            
        if config_manager.CONTEXT_CONFIG_FILE.exists():
            console.print(f"   [green]✓[/green] Context config loaded from {config_manager.CONTEXT_CONFIG_FILE}")
        else:
            console.print("   [yellow]ℹ[/yellow] No Context config found")
            console.print(f"   [dim]Context config will be auto-generated at: {config_manager.CONTEXT_CONFIG_FILE}[/dim]")
        
        # Validate each config value
        invalid_configs = []
        for key, value in config.items():
            is_valid, error_msg = config_manager.validate_config_value(key, value)
            if not is_valid:
                invalid_configs.append(f"{key}: {error_msg}")
        
        if invalid_configs:
            console.print("   [red]✗[/red] Invalid configuration values found:")
            for err in invalid_configs:
                console.print(f"     - {err}")
            all_checks_passed = False
        else:
            console.print("   [green]✓[/green] All configuration values are valid")
    except Exception as e:
        console.print(f"   [red]✗[/red] Configuration error: {e}")
        all_checks_passed = False
    
    # 2. Check database connectivity
    console.print("\n[bold]2. Checking Database Connection...[/bold]")
    try:
        _load_credentials()
        default_db = os.environ.get("CVIZ_SELECTED_DATABASE") or config.get("DEFAULT_DATABASE", "ladybugdb")
        db_source = os.environ.get("CVIZ_DB_SELECTION_SOURCE", "unknown")
        console.print(f"   Default database: {default_db} (source: {db_source})")

        if default_db != "ladybugdb":
            console.print(f"   [red]✗[/red] '{default_db}' is not supported: Code Visualizer only uses LadybugDB")
            console.print("       Run: cviz config db ladybugdb")
            all_checks_passed = False
        else:
            from code_visualizer.core import ladybugdb_unavailable_reason

            ladybug_reason = ladybugdb_unavailable_reason()
            if ladybug_reason is None:
                console.print("   [green]✓[/green] LadybugDB core (ladybug) is installed")
            else:
                console.print(f"   [red]✗[/red] {ladybug_reason}")
                console.print("       Run: pip install ladybug")
                all_checks_passed = False
    except Exception as e:
        console.print(f"   [red]✗[/red] Database check error: {e}")
        all_checks_passed = False
    
    # 3. Check tree-sitter installation
    console.print("\n[bold]3. Checking Tree-Sitter Installation...[/bold]")
    try:
        from tree_sitter import Language, Parser
        console.print("   [green]✓[/green] tree-sitter is installed")
        
        try:
            from tree_sitter_language_pack import get_language
            console.print("   [green]✓[/green] tree-sitter-language-pack is installed")
            
            from code_visualizer.utils.tree_sitter_manager import LANGUAGE_ALIASES, LANGUAGE_PACK_NAMES
            all_langs = sorted(set(LANGUAGE_ALIASES.values()))
            console.print(f"   [dim]Supported languages ({len(all_langs)}): {', '.join(all_langs)}[/dim]")
            probe_langs = ["python", "javascript", "typescript", "java", "go", "rust", "c", "cpp"]
            available, unavailable = [], []
            for lang in probe_langs:
                try:
                    pack_name = LANGUAGE_PACK_NAMES.get(lang, lang)
                    get_language(pack_name)
                    available.append(lang)
                except Exception:
                    unavailable.append(lang)
            console.print(f"   [green]✓[/green] {len(available)}/{len(probe_langs)} probed parsers OK: {', '.join(available)}")
            if unavailable:
                console.print(f"   [yellow]⚠[/yellow] Unavailable: {', '.join(unavailable)}")
                warnings_found = True
        except ImportError:
            console.print("   [red]✗[/red] tree-sitter-language-pack not installed")
            all_checks_passed = False
    except ImportError as e:
        console.print(f"   [red]✗[/red] tree-sitter not installed: {e}")
        all_checks_passed = False
    
    # 4. Check file permissions
    console.print("\n[bold]4. Checking File Permissions...[/bold]")
    try:
        config_dir = config_manager.CONFIG_DIR
        if config_dir.exists():
            console.print(f"   [green]✓[/green] Config directory exists: {config_dir}")
            
            # Check if writable
            test_file = config_dir / ".test_write"
            try:
                test_file.touch()
                test_file.unlink()
                console.print("   [green]✓[/green] Config directory is writable")
            except Exception as e:
                console.print(f"   [red]✗[/red] Config directory not writable: {e}")
                all_checks_passed = False
        else:
            console.print("   [yellow]⚠[/yellow] Config directory doesn't exist, will be created on first use")
            warnings_found = True
    except Exception as e:
        console.print(f"   [red]✗[/red] Permission check error: {e}")
        all_checks_passed = False
    
    # 5. Check cviz command availability
    console.print("\n[bold]5. Checking CViz Command...[/bold]")
    import shutil
    cviz_path = shutil.which("cviz")
    if cviz_path:
        console.print(f"   [green]✓[/green] cviz command found at: {cviz_path}")
    else:
        console.print("   [yellow]⚠[/yellow] cviz command not in PATH (using python -m cviz)")
        warnings_found = True
    
    # Final summary
    console.print("\n" + "=" * 60)
    if all_checks_passed and not warnings_found:
        console.print("[bold green]✅ All diagnostics passed! System is healthy.[/bold green]")
    elif all_checks_passed:
        console.print(
            "[bold yellow]✅ No failures, but some checks reported warnings "
            "(⚠ above).[/bold yellow]"
        )
    else:
        console.print("[bold yellow]⚠️  Some issues detected. Please review the output above.[/bold yellow]")
        console.print("\n[cyan]Common fixes:[/cyan]")
        console.print("  • For database issues: check that 'ladybug' is installed and no other cviz process holds the database")
        console.print("  • For missing packages: pip install code-visualizer")
        console.print("  • For config issues: Run 'cviz config reset'")
        raise typer.Exit(code=1)
    console.print("=" * 60 + "\n")






@app.command()
def index(
    path: Optional[str] = typer.Argument(None, help="Path to the directory or file to index. Defaults to the current directory."),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-index (delete existing and rebuild)"),
    summarize: bool = typer.Option(False, "--summarize", "-s", help="Show a summary of the indexed codebase after indexing"),
    no_progress: bool = typer.Option(False, "--no-progress", help="Disable live progress rendering during indexing."),
    embed: bool = typer.Option(False, "--embed", "-e", help="Also vectorize for the natural-language search (otherwise 'cviz graph' does it at start-up)."),
):
    """
    Indexes a directory or file by adding it to the code graph.
    If no path is provided, it indexes the current directory.

    Use --force to delete the existing index and rebuild from scratch.
    Use --summarize to display a summary after indexing.
    Use --embed to vectorize right away; without it, the vectors of the
    natural-language search are computed when 'cviz graph' starts.
    """
    _load_credentials()
    if path is None:
        path = str(Path.cwd())

    try:
        if force:
            console.print("[yellow]Force re-indexing (--force flag detected)[/yellow]")
            reindex_helper(path, None, no_progress=no_progress, embed=embed)
        else:
            index_helper(path, None, no_progress=no_progress, embed=embed)
    except typer.Exit:
        # typer.Exit subclasses RuntimeError and str() is empty, so the handler
        # below caught it, printed nothing, and returned 0 — every helper that
        # raised `typer.Exit(code=1)` (missing path, failed indexing) silently
        # became a success. Re-raise control flow before catching errors.
        raise
    except Exception as e:
        if str(e):
            console.print(f"[red]An error occurred during indexing: {e}[/red]")
        else:
            console.print("[red]An error occurred during indexing.[/red]")
        raise typer.Exit(code=1)

    if summarize:
        import os

        py_files = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['venv', '__pycache__', 'node_modules']]
            for file in files:
                if file.endswith('.py'):
                    py_files.append(os.path.join(root, file))

        total_lines = 0
        for f in py_files:
            try:
                with open(f, 'r', encoding='utf-8', errors='ignore') as fp:
                    total_lines += len(fp.readlines())
            except:
                pass

        console.print("\n[bold cyan]📊 Codebase Summary:[/bold cyan]")
        console.print(f"  • Path indexed     : [green]{path}[/green]")
        console.print(f"  • Python files     : [yellow]{len(py_files)}[/yellow]")
        console.print(f"  • Total lines      : [yellow]{total_lines}[/yellow]")
        console.print(f"\n  • Run [bold]cviz analyze complexity[/bold] to find complex functions")
        console.print(f"  • Run [bold]cviz analyze dead-code[/bold] to find unused code")
        console.print(f"  • Run [bold]cviz list[/bold] to see all indexed repositories")
        console.print("\n[dim]Tip: Use --summarize anytime after indexing to see this.[/dim]")

@app.command()
def update(
    path: Optional[str] = typer.Argument(None, help="Path to refresh. Defaults to current directory."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Reduce output when running from automation"),
    full: bool = typer.Option(False, "--full", help="Delete and rebuild the whole repository index."),
):
    """
    Bring a repository index in line with the files on disk.

    Only the files whose content differs from the graph are re-processed
    (whatever changed them: edits, branch switches, git restore). When a
    'cviz graph' server holds the database, it does the update. Git hooks
    installed with `cviz hook install` call this after checkouts, merges and
    rebases.
    """
    previous_quiet = console.quiet
    console.quiet = quiet or previous_quiet
    try:
        _load_credentials()
    finally:
        console.quiet = previous_quiet
    if path is None:
        path = str(Path.cwd())
    update_helper(path, None, quiet=quiet, full=full)

@app.command()
def clean(
):
    """
    Remove orphaned nodes and relationships from the database.
    
    This will clean up nodes that are not connected to any repository,
    helping to keep your database tidy and performant.
    """
    _load_credentials()
    clean_helper(None)

@app.command()
def stats(
    path: Optional[str] = typer.Argument(None, help="Path to show stats for. Omit for overall stats."),
):
    """
    Show indexing statistics.
    
    If a path is provided, shows stats for that specific repository.
    Otherwise, shows overall database statistics.
    """
    _load_credentials()
    if path:
        path = str(Path(path).resolve())
    stats_helper(path, None)

@app.command("setup-scip")
def setup_scip():
    """
    Check availability of SCIP indexers and provide installation hints.
    
    This command audits your system for SCIP binaries (like scip-python, scip-go)
    and checks if Docker is available for fallback indexing.
    """
    setup_scip_helper()

@app.command()
def delete(
    path: Optional[str] = typer.Argument(None, help="Path of the repository to delete from the code graph."),
    all_repos: bool = typer.Option(False, "--all", help="Delete all indexed repositories"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt (for CI/non-interactive use)"),
):
    """
    Deletes a repository from the code graph.
    
    Use --all to delete all repositories at once (requires confirmation).
    
    Examples:
        cviz delete ./my-project       # Delete specific repository
        cviz delete --all              # Delete all repositories
    """
    _load_credentials()
    
    if all_repos:
        if not config_manager.is_db_deletion_allowed():
            console.print(
                "[bold red]Error:[/bold red] Full database deletion is disabled. "
                "Set ALLOW_DB_DELETION=true in config to enable."
            )
            raise typer.Exit(code=1)

        # Delete all repositories
        services = _initialize_services()
        if not all(services[:3]):
            raise typer.Exit(code=1)
        db_manager, graph_builder, code_finder = services[:3]
        
        try:
            # Get list of repositories
            repos = code_finder.list_indexed_repositories()
            
            if not repos:
                console.print("[yellow]No repositories to delete.[/yellow]")
                return
            
            # Show what will be deleted
            console.print(f"\n[bold red]⚠️  WARNING: You are about to delete ALL {len(repos)} repositories![/bold red]\n")
            
            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Name", style="cyan")
            table.add_column("Path", style="dim")
            
            for repo in repos:
                table.add_row(repo.get("name", ""), repo.get("path", ""))
            
            console.print(table)
            console.print()
            
            # Double confirmation
            if not typer.confirm("Are you sure you want to delete ALL repositories?", default=False):
                console.print("[yellow]Deletion cancelled.[/yellow]")
                return
            
            console.print("[yellow]Please type 'delete all' to confirm:[/yellow] ", end="")
            confirmation = input()
            
            if confirmation.strip().lower() != "delete all":
                console.print("[yellow]Deletion cancelled. Confirmation text did not match.[/yellow]")
                return
            
            # Delete all repositories
            console.print("\n[cyan]Deleting all repositories...[/cyan]")
            deleted_count = 0
            
            for repo in repos:
                repo_path = repo.get("path", "")
                try:
                    graph_builder.delete_repository_from_graph(repo_path)
                    console.print(f"[green]✓[/green] Deleted: {repo.get('name', '')}")
                    deleted_count += 1
                except Exception as e:
                    console.print(f"[red]✗[/red] Failed to delete {repo.get('name', '')}: {e}")
            
            if deleted_count < len(repos):
                console.print(
                    f"\n[bold yellow]Deleted {deleted_count}/{len(repos)} repositories; "
                    "some deletions failed.[/bold yellow]"
                )
                raise typer.Exit(code=1)
            console.print(f"\n[bold green]Successfully deleted {deleted_count}/{len(repos)} repositories![/bold green]")

        finally:
            db_manager.close_driver()
    else:
        # Delete specific repository
        if not path:
            console.print("[red]Error: Please provide a path or use --all to delete all repositories[/red]")
            console.print("Usage: cviz delete <path> or cviz delete --all")
            raise typer.Exit(code=1)

        if not config_manager.is_db_deletion_allowed():
            console.print(
                "[bold red]Error:[/bold red] Repository deletion is disabled. "
                "Set ALLOW_DB_DELETION=true in config to enable."
            )
            raise typer.Exit(code=1)

        # `--all` demands a typer.confirm *and* typing "delete all", while a
        # single delete went straight through — irreversibly dropping a
        # repository's entire graph (potentially an hour of indexing) on a
        # typo, with no prompt, no --yes flag and no undo. The asymmetry was
        # actively misleading: anyone who had seen the heavy --all guardrails
        # would reasonably assume single deletes were guarded too.
        if not yes:
            resolved = Path(path).expanduser().resolve()
            console.print(
                f"[bold yellow]About to delete the graph for:[/bold yellow] {resolved}"
            )
            console.print("[dim]This is irreversible; re-indexing is the only way back.[/dim]")
            if not typer.confirm("Proceed?", default=False):
                console.print("[yellow]Deletion cancelled.[/yellow]")
                raise typer.Exit(code=1)

        delete_helper(path, None)


@app.command()
def report(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path. Defaults to CVIZ_REPORT.md in the current directory."),
    java: bool = typer.Option(False, "--java", "-j", help="Include Spring/Maven Java sections."),
    repo: Optional[str] = typer.Option(None, "--repo", "-r", help="Repository root to scope the report to. Defaults to auto-detection from the current directory."),
):
    """
    Generate a CVIZ_REPORT.md with god nodes, complexity, cross-module connections, and suggested queries.

    Use --java to also include Spring endpoint tables, bean stereotype counts,
    and Maven module dependency summaries.

    Examples:
        cviz report
        cviz report --output /tmp/my_report.md
        cviz report --java
    """
    _load_credentials()
    output_path = Path(output) if output else Path.cwd() / "CVIZ_REPORT.md"
    db_manager, _, _, _ = _initialize_services()
    try:
        from code_visualizer.tools.report_generator import generate_report
        # Without --repo the generator silently picks the repo with the most
        # indexed files, which is disclosed only in the report body.
        scoped_repo = Path(repo).expanduser().resolve().as_posix() if repo else None
        report_text = generate_report(
            db_manager,
            output_path=output_path,
            include_java=java,
            repo_path=scoped_repo,
        )
        console.print(f"[green]✓[/green] Report written to [bold]{output_path}[/bold]")
        # Print a short preview (first ~40 lines)
        preview_lines = report_text.splitlines()[:40]
        console.print("\n".join(preview_lines))
        if len(report_text.splitlines()) > 40:
            console.print(f"[dim]... ({len(report_text.splitlines())} lines total, see {output_path})[/dim]")
    except Exception as exc:
        console.print(f"[red]Report generation failed:[/red] {exc}")
        raise typer.Exit(code=1)
    finally:
        try:
            db_manager.close_driver()
        except Exception:
            pass


@app.command()
def visualize(
    repo: Optional[str] = typer.Option(None, "--repo", "-r", help="Path to the repository to visualize."),
    # `-h` is --help everywhere else in the CLI; use -H so `cviz visualize -h`
    # doesn't fail with "Option '-h' requires an argument".
    host: str = typer.Option("127.0.0.1", "--host", "-H", help="Host interface to bind to."),
    port: int = typer.Option(DEFAULT_GRAPH_PORT, "--port", "-p", help="Port to run the visualizer server on."),
):
    """
    Launches the interactive UI to visualize the code graph.
    """
    _load_credentials()
    visualize_helper(repo, host, port, None)

@app.command("graph")
def graph(
    target: Optional[str] = typer.Argument(
        None, help="Artifact to show first, as FILE or FILE:LINE, or a repository folder to serve (optional)."
    ),
    watch_path: Optional[str] = typer.Option(
        None, "--watch", "-w",
        help="Also watch this repository in-process and push live updates to open pages.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-H", help="Host interface to bind to."),
    port: Optional[int] = typer.Option(
        None, "--port", "-p",
        help=f"Port to serve on (default {DEFAULT_GRAPH_PORT}, or the next free one: one server per repository).",
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser tab."),
    changes: Optional[str] = typer.Option(
        None, "--changes", "-c", metavar="BRANCH",
        help="Open the Changes view: what the current state adds or modifies against BRANCH, and who is impacted.",
    ),
    poll: bool = typer.Option(False, "--poll", help="Use the polling observer for --watch."),
    full_resync_hours: float = typer.Option(
        0, "--full-resync-hours", help="With --watch: full resync every N hours (0 = never)."
    ),
    path_map: Optional[str] = typer.Option(
        None, "--path-map",
        help="Index built elsewhere: 'indexed/prefix=local/prefix;...' (env CVIZ_PATH_MAP).",
    ),
):
    """
    Serve the dependency-graph page linked to your IDE (/graph).

    The page shows an artifact's callers, callees and inheritance (depth 1-5);
    clicking a node recentres, right-click opens the code in VS Code, Rider or
    Zed. IDEs open the page with 'cviz-open FILE:LINE'.

    Examples:
        cviz graph --watch .                # this repository, live: http://localhost:9324/graph
        cviz graph                          # serve the current repository's index
        cviz graph src/app/orders.py:42     # open that artifact right away
        cviz graph ~/code/shop              # serve that repository's index
        cviz graph --watch . --changes origin/develop   # review this branch against develop
    """
    _load_credentials()
    if path_map:
        os.environ["CVIZ_PATH_MAP"] = path_map
    graph_helper(
        target,
        host=host,
        port=port,
        watch_path=watch_path,
        open_browser=not no_browser,
        use_polling=poll or None,
        full_resync_hours=full_resync_hours,
        changes=changes,
    )


@app.command("embed")
def embed(
    path: Optional[str] = typer.Argument(None, help="Indexed repository (or a path inside it). Default: current directory."),
    all_repos: bool = typer.Option(False, "--all", help="Vectorize every indexed repository."),
):
    """
    Vectorize indexed code for the natural-language search of the graph page.

    Only artifacts whose text changed since the last run are embedded. The
    model is set in the page (Settings > AI) or with 'cviz config set
    SEARCH_EMBED_PROVIDER local|openai'.
    """
    from .cli_helpers import embed_helper

    embed_helper(path, all_repos, None)


@app.command("open")
def open_in_graph(
    location: str = typer.Argument(..., help="FILE, FILE:LINE or FILE:LINE:COLUMN"),
    server: Optional[str] = typer.Option(None, "--server", help="Graph server URL (env CVIZ_GRAPH_URL; default: the server of the file's repository)."),
    new_tab: bool = typer.Option(False, "--new-tab", help="Always open a new tab."),
    no_start: bool = typer.Option(False, "--no-start", help="Do not start 'cviz graph' if it is not running."),
    print_only: bool = typer.Option(False, "--print", help="Only print the page URL."),
):
    """
    Show FILE:LINE in the dependency-graph page (same as the 'cviz-open' script).
    """
    from .open_in_graph import main as open_main

    argv = [location]
    if server:
        argv += ["--server", server]
    if new_tab:
        argv.append("--new-tab")
    if no_start:
        argv.append("--no-start")
    if print_only:
        argv.append("--print")
    raise typer.Exit(code=open_main(argv))


# ============================================================================
# IDE COMMAND GROUP - dependency-graph integration for Zed / JetBrains
# ============================================================================

ide_app = typer.Typer(help="Install the dependency-graph shortcut in Zed or JetBrains IDEs")
app.add_typer(ide_app, name="ide")


@ide_app.command("zed")
def ide_zed(
    repo: str = typer.Argument(".", help="Repository whose .zed/tasks.json receives the task."),
):
    """
    Add the "open in dependency graph" task to REPO/.zed/tasks.json.

    Commit that file to share the task with the team; each developer then adds
    the printed key binding to ~/.config/zed/keymap.json once.
    """
    from .ide_setup import ZED_KEYMAP_SNIPPET, install_zed_task, launcher

    program, lead = launcher(absolute=False)
    try:
        console.print(f"[green]{install_zed_task(Path(repo).resolve(), program, lead)}[/green]")
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print("Add this key binding to ~/.config/zed/keymap.json (Zed › Settings › Open Key Bindings):")
    console.print(ZED_KEYMAP_SNIPPET, markup=False, highlight=False)


@ide_app.command("jetbrains")
def ide_jetbrains(
    product: Optional[str] = typer.Option(None, "--product", help="Only this IDE, e.g. Rider or IntelliJIdea."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be written."),
):
    """
    Install the "Open in Dependency Graph" External Tool in JetBrains IDEs.

    Writes tools/CViz.xml into every JetBrains IDE configuration found (Rider,
    IntelliJ, WebStorm…). Restart the IDE, then bind a shortcut in
    Settings › Keymap › External Tools › CViz.
    """
    from .ide_setup import install_jetbrains_tool, jetbrains_config_dirs, launcher

    dirs = jetbrains_config_dirs(product)
    if not dirs:
        console.print("[yellow]No JetBrains IDE configuration found. Open the IDE once, then retry.[/yellow]")
        raise typer.Exit(code=1)
    program, lead = launcher(absolute=True)
    for config_dir in dirs:
        console.print(install_jetbrains_tool(config_dir, program, lead, dry_run=dry_run))
    if not dry_run:
        console.print("Restart the IDE, then: Settings › Keymap › External Tools › CViz › "
                      "Open in Dependency Graph › Add Keyboard Shortcut (e.g. Ctrl+Alt+G).")


@app.command("list")
def list_repositories(
):
    """
    List all indexed repositories.
    
    Shows all projects and packages that have been indexed in the code graph.
    """
    _load_credentials()
    list_repos_helper(None)

@app.command(name="add-package")
def add_package(
    package_name: str = typer.Argument(..., help="Name of the package to add."),
    language: str = typer.Argument(..., help="Language of the package."),
):
    """
    Adds a package to the code graph.
    """
    _load_credentials()
    add_package_helper(package_name, language, None)

# ============================================================================
# WATCH COMMAND GROUP - Live File Monitoring
# ============================================================================

@app.command()
def watch(
    path: str = typer.Argument(".", help="Path to the directory to watch. Defaults to current directory."),
    poll: bool = typer.Option(
        False,
        "--poll",
        help="Use watchdog's polling observer for Docker bind mounts and network filesystems.",
    ),
    sync_on_start: bool = typer.Option(
        False,
        "--sync-on-start",
        help=(
            "Re-parse every already-indexed file before watching (full reconcile). "
            "Defaults off; use 'cviz index --force' for a full re-index."
        ),
    ),
    catch_up: bool = typer.Option(
        True,
        "--catch-up/--no-catch-up",
        help=(
            "Before watching, update only the files added, changed or deleted "
            "since the last update (cheap; on by default)."
        ),
    ),
    full_resync_hours: float = typer.Option(
        0,
        "--full-resync-hours",
        help="Also re-synchronise the whole repository every N hours (0 = never).",
    ),
):
    """
    Watch a directory for file changes and automatically update the code graph.
    
    This command runs in the foreground and monitors the specified directory
    for any file changes. When changes are detected, the code graph is
    automatically updated.
    
    The watcher will:
    - Perform an initial scan if the directory is not yet indexed
    - Attach immediately for already-indexed directories unless --sync-on-start is passed
    - Monitor for file creation, modification, deletion, and moves
    - Automatically re-index affected files and update relationships
    
    Press Ctrl+C to stop watching.
    
    Examples:
        cviz watch .                    # Watch current directory
        cviz watch /path/to/project     # Watch specific directory
        cviz watch --poll .             # Use polling for Docker/NFS/SMB mounts
        cviz watch --sync-on-start .    # Reconcile current files before watching
        cviz w .                        # Using shortcut alias

    Set CVIZ_WATCH_POLLING=1 to use polling without passing --poll.
    """
    _load_credentials()
    watch_helper(
        path,
        None,
        use_polling=poll or None,
        sync_on_start=sync_on_start,
        catch_up=catch_up,
        full_resync_hours=full_resync_hours,
    )

@app.command()
def unwatch(
    path: str = typer.Argument(..., help="Path to stop watching"),
):
    """
    [MCP only] Stop watching a directory for changes.

    Not supported from the CLI: this cannot reach a watcher running in another
    process. Press Ctrl+C in the 'cviz watch' terminal, or use the
    'unwatch_directory' MCP tool.
    
    Note: This command is primarily for MCP server mode.
    For CLI watch mode, simply press Ctrl+C in the watch terminal.
    
    Examples:
        cviz unwatch /path/to/project
    """
    _load_credentials()
    unwatch_helper(path)

@app.command("watching")
def watching(
):
    """
    [MCP only] List all directories currently being watched for changes.

    Not supported from the CLI: this cannot reach a watcher running in another
    process. Use the 'list_watched_paths' MCP tool.
    
    Note: This command is primarily for MCP server mode.
    For CLI watch mode, check the terminal where you ran 'cviz watch'.
    
    Examples:
        cviz watching
    """
    _load_credentials()
    list_watching_helper()



# ============================================================================
# FIND COMMAND GROUP - Code Search & Discovery
# ============================================================================

find_app = typer.Typer(help="Find and search code elements")
app.add_typer(find_app, name="find")

@find_app.command("name")
def find_by_name(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Name to search for"),
    type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by type (function, class, file, module)"),
    fuzzy: Optional[bool] = typer.Option(None, "--fuzzy/--no-fuzzy", help="Enable/disable fuzzy matching for this command. Overrides the FUZZY_SEARCH config value (default: true)."),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Find code elements by name.

    Fuzzy matching is enabled by default (configurable via the FUZZY_SEARCH
    config key, or per-invocation with --fuzzy / --no-fuzzy).

    Examples:
        cviz find name MyClass
        cviz find name calculate --type function
        cviz find name MyClass --no-fuzzy
        cviz find name MyClass --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]

    # Resolve effective fuzzy setting: CLI flag wins, else config, else true.
    if fuzzy is None:
        from code_visualizer.cli.config_manager import load_config
        cfg_value = load_config().get("FUZZY_SEARCH", "true")
        fuzzy_search = str(cfg_value).strip().lower() == "true"
    else:
        fuzzy_search = fuzzy

    try:
        results = []

        _VALID_FIND_TYPES = {
            'all', 'function', 'class', 'variable', 'module', 'file',
        }
        if type is not None and type.lower() not in _VALID_FIND_TYPES:
            console.print(
                f"[bold red]Invalid --type '{type}'.[/bold red] "
                f"Must be one of: {', '.join(sorted(_VALID_FIND_TYPES))}"
            )
            raise typer.Exit(code=1)

        # Search based on type filter
        if type is None or type.lower() == 'all':
            funcs = code_finder.find_by_function_name(name, fuzzy_search=fuzzy_search)
            classes = code_finder.find_by_class_name(name, fuzzy_search=fuzzy_search)
            variables = code_finder.find_by_variable_name(name)
            modules = code_finder.find_by_module_name(name)
            imports = code_finder.find_imports(name)

            for f in funcs: f['type'] = 'Function'
            for c in classes: c['type'] = 'Class'
            for v in variables: v['type'] = 'Variable'
            for m in modules: m['type'] = 'Module'; m['path'] = m.get('name', 'External') # Modules might differ
            for i in imports: 
                i['type'] = 'Import'
                i['name'] = i.get('alias') or i.get('imported_name')
            
            results.extend(funcs)
            results.extend(classes)
            results.extend(variables)
            results.extend(modules)
            results.extend(imports)

            # Also search Interface, Trait, Struct, Enum nodes (PHP, Rust, Go, etc.)
            with db_manager.get_driver().session() as session:
                for label in ['Interface', 'Trait', 'Struct', 'Enum']:
                    res = session.run(
                        f"MATCH (n:{label}) WHERE n.name = $name RETURN n.name as name, n.path as path, n.line_number as line_number",
                        name=name
                    )
                    for record in res:
                        row = dict(record)
                        row['type'] = label
                        results.append(row)
        
        elif type.lower() == 'function':
            results = code_finder.find_by_function_name(name, fuzzy_search=fuzzy_search)
            for r in results: r['type'] = 'Function'

        elif type.lower() == 'class':
            results = code_finder.find_by_class_name(name, fuzzy_search=fuzzy_search)
            for r in results: r['type'] = 'Class'
            
        elif type.lower() == 'variable':
            results = code_finder.find_by_variable_name(name)
            for r in results: r['type'] = 'Variable'

        elif type.lower() == 'module':
            results = code_finder.find_by_module_name(name)
            for r in results: 
                r['type'] = 'Module'
                r['path'] = r.get('name')
            
        elif type.lower() == 'file':
            # Quick query for file
            with db_manager.get_driver().session() as session:
                res = session.run("MATCH (n:File) WHERE n.name = $name RETURN n.name as name, n.path as path, n.is_dependency as is_dependency", name=name)
                results = [dict(record) for record in res]
                for r in results: r['type'] = 'File'
        
        if not results:
            find_result_console.print(f"[yellow]No code elements found with name '{name}'[/yellow]")
            return
        
        # Check if visual mode is enabled
        if check_visual_flag(ctx, visual):
            visualize_search_results(results, name, search_type="name")
            return
            
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="bold blue")
        table.add_column("Location", style="dim", overflow="fold")
        
        for res in results:
            path = res.get('path', '') or ''
            line_str = str(res.get('line_number', ''))
            location_str = f"{path}:{line_str}" if line_str else path

            table.add_row(
                res.get('name', ''),
                res.get('type', 'Unknown'),
                location_str
            )
            
        find_result_console.print(f"[cyan]Found {len(results)} matches for '{name}':[/cyan]")
        find_result_console.print(table)
    finally:
        db_manager.close_driver()

@find_app.command("pattern")
def find_by_pattern(
    ctx: typer.Context,
    pattern: str = typer.Argument(..., help="Substring pattern to search (fuzzy search fallback)"),
    case_sensitive: bool = typer.Option(False, "--case-sensitive", "-C", help="Case-sensitive search"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Find code elements using substring matching.
    
    Examples:
        cviz find pattern "Auth"       # Finds Auth, Authentication, Authorize...
        cviz find pattern "process_"   # Finds process_data, process_request...
        cviz find pattern "Auth" --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        with db_manager.get_driver().session() as session:
            # Search Functions, Classes, and Modules
            # LadybugDB: CONTAINS rather than a regex match
            
            if not case_sensitive:
                query = """
                    MATCH (n)
                    WHERE (n:Function OR n:Class OR n:Module OR n:Variable OR n:Interface OR n:Trait OR n:Struct OR n:Enum) AND toLower(n.name) CONTAINS toLower($pattern)
                    RETURN 
                        labels(n)[0] as type,
                        n.name as name,
                        n.path as path,
                        n.line_number as line_number,
                        n.is_dependency as is_dependency
                    ORDER BY n.is_dependency ASC, n.name
                    LIMIT 50
                """
            else:
                 query = """
                    MATCH (n)
                    WHERE (n:Function OR n:Class OR n:Module OR n:Variable OR n:Interface OR n:Trait OR n:Struct OR n:Enum) AND n.name CONTAINS $pattern
                    RETURN 
                        labels(n)[0] as type,
                        n.name as name,
                        n.path as path,
                        n.line_number as line_number,
                        n.is_dependency as is_dependency
                    ORDER BY n.is_dependency ASC, n.name
                    LIMIT 50
                """
            
            result = session.run(query, pattern=pattern)
            
            results = [dict(record) for record in result]
        
        if not results:
            find_result_console.print(f"[yellow]No matches found for pattern '{pattern}'[/yellow]")
            return
        
        # Check if visual mode is enabled
        if check_visual_flag(ctx, visual):
            visualize_search_results(results, pattern, search_type="pattern")
            return
            
        if not case_sensitive and any(c in pattern for c in "*?["):
             find_result_console.print("[yellow]Note: Wildcards/Regex are not fully supported in this mode. Performing substring search.[/yellow]")

        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="blue")
        table.add_column("Location", style="dim", overflow="fold")
        table.add_column("Source", style="yellow")
        
        for res in results:
            path = res.get('path', '') or ''
            line_str = str(res.get('line_number', '') if res.get('line_number') is not None else '')
            location_str = f"{path}:{line_str}" if line_str else path

            table.add_row(
                res.get('name', ''),
                res.get('type', 'Unknown'),
                location_str,
                "📦 Dependency" if res.get('is_dependency') else "📝 Project"
            )
            
        find_result_console.print(f"[cyan]Found {len(results)} matches for pattern '{pattern}':[/cyan]")
        find_result_console.print(table)
    finally:
        db_manager.close_driver()

@find_app.command("type")
def find_by_type(
    ctx: typer.Context,
    element_type: str = typer.Argument(..., help="Type to search for (function, class, file, module)"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum results to return"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Find all elements of a specific type.
    
    Examples:
        cviz find type class
        cviz find type function --limit 100
        cviz find type class --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        results = code_finder.find_by_type(element_type, limit)
        
        if not results:
            find_result_console.print(f"[yellow]No elements found of type '{element_type}'[/yellow]")
            return
        
        # Add type to results for visualization
        for r in results:
            r['type'] = element_type.capitalize()
        
        # Check if visual mode is enabled
        if check_visual_flag(ctx, visual):
            visualize_search_results(results, element_type, search_type="type")
            return
            
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Name", style="cyan")
        table.add_column("Location", style="dim", overflow="fold")
        table.add_column("Source", style="yellow")
        
        for res in results:
            path = res.get('path', '') or ''
            line_str = str(res.get('line_number', ''))
            location_str = f"{path}:{line_str}" if line_str else path
            
            table.add_row(
                res.get('name', ''),
                location_str,
                "📦 Dependency" if res.get('is_dependency') else "📝 Project"
            )
            
        find_result_console.print(f"[cyan]Found {len(results)} {element_type}s:[/cyan]")
        find_result_console.print(table)
    finally:
        db_manager.close_driver()

@find_app.command("variable")
def find_by_variable(
    name: str = typer.Argument(..., help="Variable name to search for"),
):
    """
    Find variables by name.
    
    Examples:
        cviz find variable MAX_RETRIES
        cviz find variable config
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        results = code_finder.find_by_variable_name(name)
        
        if not results:
            find_result_console.print(f"[yellow]No variables found with name '{name}'[/yellow]")
            return
            
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Name", style="cyan")
        table.add_column("Location", style="dim", overflow="fold")
        table.add_column("Context", style="yellow")
        
        for res in results:
            path = res.get('path', '') or ''
            line_str = str(res.get('line_number', ''))
            location_str = f"{path}:{line_str}" if line_str else path

            table.add_row(
                res.get('name', ''),
                location_str,
                res.get('context', '') or 'module'
            )
            
        find_result_console.print(f"[cyan]Found {len(results)} variable(s) named '{name}':[/cyan]")
        find_result_console.print(table)
    finally:
        db_manager.close_driver()

@find_app.command("content")
def find_by_content_search(
    query: str = typer.Argument(..., help="Text to search for in source code and docstrings"),
):
    """
    Search code content (source and docstrings) using full-text index.
    
    Examples:
        cviz find content "error 503"
        cviz find content "TODO: refactor"
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        try:
            results = code_finder.find_by_content(query)
        except Exception:
            raise
        
        if not results:
            find_result_console.print(f"[yellow]No content matches found for '{query}'[/yellow]")
            return
            
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="blue")
        table.add_column("Location", style="dim", overflow="fold")
        
        for res in results:
            path = res.get('path', '') or ''
            line_str = str(res.get('line_number', ''))
            location_str = f"{path}:{line_str}" if line_str else path

            table.add_row(
                res.get('name', ''),
                res.get('type', 'Unknown'),
                location_str
            )
            
        find_result_console.print(f"[cyan]Found {len(results)} content match(es) for '{query}':[/cyan]")
        find_result_console.print(table)
    finally:
        db_manager.close_driver()

@find_app.command("decorator")
def find_by_decorator_search(
    decorator: str = typer.Argument(..., help="Decorator name to search for"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Specific file path"),
):
    """
    Find functions with a specific decorator.
    
    Examples:
        cviz find decorator app.route
        cviz find decorator test --file tests/test_main.py
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        req_limit = get_tool_result_limit("find_functions_by_decorator")
        results = code_finder.find_functions_by_decorator(decorator, file, limit=req_limit + 1 if req_limit is not None else None)
        truncated = bool(req_limit and len(results) > req_limit)
        if truncated:
            results = results[:req_limit]
        
        if not results:
            find_result_console.print(f"[yellow]No functions found with decorator '@{decorator}'[/yellow]")
            return
            
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Function", style="cyan")
        table.add_column("Location", style="dim", overflow="fold")
        table.add_column("Decorators", style="yellow")
        
        for res in results:
            decorators_str = ", ".join(res.get('decorators', []))
            path = res.get('path', '') or ''
            line_str = str(res.get('line_number', ''))
            location_str = f"{path}:{line_str}" if line_str else path

            table.add_row(
                res.get('function_name', ''),
                location_str,
                decorators_str
            )
            
        find_result_console.print(f"[cyan]Found {len(results)} function(s) with decorator '@{decorator}':[/cyan]")
        find_result_console.print(table)
        if truncated:
            find_result_console.print(f"[dim]... truncated ({req_limit} shown), more exist[/dim]")
    finally:
        db_manager.close_driver()

@find_app.command("argument")
def find_by_argument_search(
    argument: str = typer.Argument(..., help="Argument/parameter name or type to search for"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Specific file path"),
):
    """
    Find functions that take a specific argument/parameter name or type.
    
    Examples:
        cviz find argument password
        cviz find argument user_id --file src/auth.py
        cviz find argument OrderFilter
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        req_limit = get_tool_result_limit("find_functions_by_argument")
        results = code_finder.find_functions_by_argument(argument, file, limit=req_limit + 1 if req_limit is not None else None)
        truncated = bool(req_limit and len(results) > req_limit)
        if truncated:
            results = results[:req_limit]
        
        if not results:
            find_result_console.print(f"[yellow]No functions found with argument name or type '{argument}'[/yellow]")
            return
            
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Function", style="cyan")
        table.add_column("Location", style="dim", overflow="fold")
        
        for res in results:
            path = res.get('path', '') or ''
            line_str = str(res.get('line_number', ''))
            location_str = f"{path}:{line_str}" if line_str else path

            table.add_row(
                res.get('function_name', ''),
                location_str
            )
            
        find_result_console.print(f"[cyan]Found {len(results)} function(s) with argument '{argument}':[/cyan]")
        find_result_console.print(table)
        if truncated:
            find_result_console.print(f"[dim]... truncated ({req_limit} shown), more exist[/dim]")
    finally:
        db_manager.close_driver()


# ============================================================================
# ANALYZE COMMAND GROUP - Code Analysis & Relationships
# ============================================================================

analyze_app = typer.Typer(help="Analyze code relationships, dependencies, and quality")
app.add_typer(analyze_app, name="analyze")

@analyze_app.command("calls")
def analyze_calls(
    ctx: typer.Context,
    function: str = typer.Argument(..., help="Function name to analyze"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Specific file path"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Show what functions this function calls (callees).
    
    Example:
        cviz analyze calls process_data
        cviz analyze calls process_data --file src/main.py
        cviz analyze calls process_data --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        req_limit = get_tool_result_limit("find_callees")
        results = code_finder.what_does_function_call(function, file, limit=req_limit + 1 if req_limit is not None else None)
        truncated = bool(req_limit and len(results) > req_limit)
        if truncated:
            results = results[:req_limit]
        
        if not results:
            analyze_result_console.print(f"[yellow]No function calls found for '{function}'[/yellow]")
            return
        
        # Check if visual mode is enabled
        if check_visual_flag(ctx, visual):
            visualize_call_graph(results, function, direction="outgoing")
            return
        
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Called Function", style="cyan")
        table.add_column("Location", style="dim", overflow="fold")
        table.add_column("Type", style="yellow")
        
        for result in results:
            path = result.get("called_file_path", "")
            line_str = str(result.get("called_line_number", ""))
            location_str = f"{path}:{line_str}" if line_str else path

            table.add_row(
                result.get("called_function", ""),
                location_str,
                "📦 Dependency" if result.get("called_is_dependency") else "📝 Project"
            )
        
        analyze_result_console.print(f"\n[bold cyan]Function '{function}' calls:[/bold cyan]")
        analyze_result_console.print(table)
        if truncated:
            analyze_result_console.print(f"\n[dim]Total: {len(results)} function(s) (truncated, {req_limit}+ exist)[/dim]")
        else:
            analyze_result_console.print(f"\n[dim]Total: {len(results)} function(s)[/dim]")
    finally:
        db_manager.close_driver()

@analyze_app.command("callers")
def analyze_callers(
    ctx: typer.Context,
    function: str = typer.Argument(..., help="Function name to analyze"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Specific file path"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Show what functions call this function.
    
    Example:
        cviz analyze callers process_data
        cviz analyze callers process_data --file src/main.py
        cviz analyze callers process_data --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        req_limit = get_tool_result_limit("find_callers")
        results = code_finder.who_calls_function(function, file, limit=req_limit + 1 if req_limit is not None else None)
        truncated = bool(req_limit and len(results) > req_limit)
        if truncated:
            results = results[:req_limit]
        
        if not results:
            analyze_result_console.print(f"[yellow]No callers found for '{function}'[/yellow]")
            return
        
        # Check if visual mode is enabled
        if check_visual_flag(ctx, visual):
            visualize_call_graph(results, function, direction="incoming")
            return
        
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Caller Function", style="cyan")
        table.add_column("Location", style="green")
        table.add_column("Call Type", style="yellow")

        
        for result in results:
            path = result.get("caller_file_path", "")
            line_number = result.get("caller_line_number")

            location = f"{path}:{line_number}" if line_number else path

            table.add_row(
                result.get("caller_function", ""),
                location,
                "📦 Dependency" if result.get("caller_is_dependency") else "📝 Project"
                )
        
        analyze_result_console.print(f"\n[bold cyan]Functions that call '{function}':[/bold cyan]")
        analyze_result_console.print(table)
        if truncated:
            analyze_result_console.print(f"\n[dim]Total: {len(results)} caller(s) (truncated, {req_limit}+ exist)[/dim]")
        else:
            analyze_result_console.print(f"\n[dim]Total: {len(results)} caller(s)[/dim]")
    finally:
        db_manager.close_driver()

@analyze_app.command("chain")
def analyze_chain(
    ctx: typer.Context,
    from_func: str = typer.Argument(..., help="Starting function"),
    to_func: str = typer.Argument(..., help="Target function"),
    max_depth: int = typer.Option(5, "--depth", "-d", help="Maximum call chain depth"),
    from_file: Optional[str] = typer.Option(None, "--from-file", help="File for starting function"),
    to_file: Optional[str] = typer.Option(None, "--to-file", help="File for target function"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Show call chain between two functions.
    
    Example:
        cviz analyze chain main process_data --depth 10
        cviz analyze chain main process --from-file main.py --to-file utils.py
        cviz analyze chain main process_data --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        req_limit = get_tool_result_limit("call_chain")
        results = code_finder.find_function_call_chain(from_func, to_func, max_depth, from_file, to_file, limit=req_limit + 1 if req_limit is not None else None)
        truncated = bool(req_limit and len(results) > req_limit)
        if truncated:
            results = results[:req_limit]
        
        if not results:
            analyze_result_console.print(f"[yellow]No call chain found between '{from_func}' and '{to_func}' within depth {max_depth}[/yellow]")
            return
        
        # Check if visual mode is enabled
        if check_visual_flag(ctx, visual):
            visualize_call_chain(results, from_func, to_func)
            return
        
        for idx, chain in enumerate(results, 1):
            analyze_result_console.print(f"\n[bold cyan]Call Chain #{idx} (length: {chain.get('chain_length', 0)}):[/bold cyan]")
            
            functions = chain.get('function_chain', [])
            call_details = chain.get('call_details', [])
            
            for i, func in enumerate(functions):
                indent = "  " * i
                
                # Print function
                analyze_result_console.print(f"{indent}[cyan]{func.get('name', 'Unknown')}[/cyan] [dim]({func.get('path', '')}:{func.get('line_number', '')})[/dim]")
                
                # If there is a next step, print the connecting call detail
                if i < len(functions) - 1 and i < len(call_details):
                    detail = call_details[i]
                    line = detail.get('call_line', '?')
                    
                    # Format args for display
                    args_info = ""
                    args_val = detail.get('args', [])
                    if args_val:
                        if isinstance(args_val, list):
                            # Filter legacy punctuation just in case
                            clean_args = [str(a) for a in args_val if str(a) not in ('(', ')', ',')]
                            args_str = ", ".join(clean_args)
                        else:
                            args_str = str(args_val)
                            
                        # Truncate if too long
                        if len(args_str) > 50:
                            args_str = args_str[:47] + "..."
                        args_info = f" [dim]({args_str})[/dim]"
                    
                    analyze_result_console.print(f"{indent}  ⬇ [dim]calls at line {line}[/dim]{args_info}")
    finally:
        db_manager.close_driver()

@analyze_app.command("kotlin-call-audit")
def analyze_kotlin_call_audit(
    repo_path: Optional[str] = typer.Option(None, "--repo-path", "-r", help="Limit audit to paths under this repository root"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum examples/top names to show"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
    fail_on_ambiguity: bool = typer.Option(False, "--fail-on-ambiguity", help="Exit non-zero if any ambiguous Kotlin call groups are found"),
):
    """
    Audit Kotlin function call edges for multi-target callsite ambiguity.

    Example:
        cviz analyze kotlin-call-audit --context elrond-stable --fail-on-ambiguity
        cviz analyze kotlin-call-audit --json
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, _, code_finder = services[:3]

    try:
        result = code_finder.audit_kotlin_call_ambiguity(repo_path=repo_path, limit=limit)
        if json_output:
            analyze_result_console.print_json(json.dumps(result))
        else:
            analyze_result_console.print("\n[bold cyan]Kotlin CALLS ambiguity audit[/bold cyan]")
            summary = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
            summary.add_column("Metric", style="cyan")
            summary.add_column("Value", style="green")
            summary.add_row("Kotlin fn→fn CALLS edges", str(result["kotlin_fn_to_fn_edges"]))
            summary.add_row("Ambiguous groups", str(result["ambiguous_groups"]))
            summary.add_row("Ambiguous edges", str(result["ambiguous_edges"]))
            analyze_result_console.print(summary)

            if result["examples"]:
                examples = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
                examples.add_column("Callsite", style="cyan", overflow="fold")
                examples.add_column("Call", style="yellow", overflow="fold")
                examples.add_column("Targets", style="green", overflow="fold")
                for example in result["examples"]:
                    targets = "\n".join(
                        f"{target['context'] or ''}:{target['line_number']} {target['path']}"
                        for target in example["targets"]
                    )
                    examples.add_row(
                        f"{example.get('caller_name')} {example.get('caller_path')}:{example.get('call_line')}",
                        str(example.get("full_call_name") or ""),
                        targets,
                    )
                analyze_result_console.print(examples)
            else:
                analyze_result_console.print("[green]No ambiguous Kotlin call groups found.[/green]")

        if fail_on_ambiguity and result["ambiguous_groups"]:
            raise typer.Exit(1)
    finally:
        db_manager.close_driver()

@analyze_app.command("deps")
def analyze_dependencies(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="Module name"),
    show_external: bool = typer.Option(True, "--external/--no-external", help="Show external dependencies"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Show dependencies and imports for a module.
    
    Example:
        cviz analyze deps numpy
        cviz analyze deps mymodule --no-external
        cviz analyze deps mymodule --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        results = code_finder.find_module_dependencies(target)
        
        if not results.get('importers') and not results.get('imports'):
            analyze_result_console.print(f"[yellow]No dependency information found for '{target}'[/yellow]")
            return
        
        # Check if visual mode is enabled
        if check_visual_flag(ctx, visual):
            visualize_dependencies(results, target)
            return
        
        importers = results.get('importers') or []
        if not show_external:
            importers = [row for row in importers if not row.get('file_is_dependency')]

        if importers:
            analyze_result_console.print(f"\n[bold cyan]Files that import '{target}':[/bold cyan]")
            table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
            table.add_column("Location", style="cyan", overflow="fold")

            for imp in importers:
                path = imp.get('importer_file_path', '')
                line_str = str(imp.get('import_line_number', ''))
                location_str = f"{path}:{line_str}" if line_str else path
                table.add_row(location_str)
            analyze_result_console.print(table)

        imports = results.get('imports') or []
        if imports:
            analyze_result_console.print(f"\n[bold cyan]Modules commonly imported alongside '{target}':[/bold cyan]")
            imp_table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
            imp_table.add_column("Module", style="cyan")
            imp_table.add_column("Alias", style="dim")
            for row in imports:
                imp_table.add_row(
                    str(row.get('imported_module', row.get('imported_name', ''))),
                    str(row.get('import_alias', '') or ''),
                )
            analyze_result_console.print(imp_table)
    finally:
        db_manager.close_driver()

@analyze_app.command("tree")
def analyze_inheritance_tree(
    ctx: typer.Context,
    class_name: str = typer.Argument(..., help="Class name"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Specific file path"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Show inheritance hierarchy for a class.
    
    Example:
        cviz analyze tree MyClass
        cviz analyze tree MyClass --file src/models.py
        cviz analyze tree MyClass --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        results = code_finder.find_class_hierarchy(class_name, file)
        
        # Check if visual mode is enabled (check for any hierarchy data)
        has_hierarchy = results.get('parent_classes') or results.get('child_classes')
        if check_visual_flag(ctx, visual):
            if has_hierarchy:
                visualize_inheritance_tree(results, class_name)
            else:
                analyze_result_console.print(f"[yellow]No inheritance hierarchy to visualize for '{class_name}'[/yellow]")
            return
        
        analyze_result_console.print(f"\n[bold cyan]Class Hierarchy for '{class_name}':[/bold cyan]\n")
        
        # Show parent classes
        if results.get('parent_classes'):
            analyze_result_console.print("[bold yellow]Parents (inherits from):[/bold yellow]")
            for parent in results['parent_classes']:
                analyze_result_console.print(f"  ⬆ [cyan]{parent.get('parent_class', '')}[/cyan] [dim]({parent.get('parent_file_path', '')}:{parent.get('parent_line_number', '')})[/dim]")
        else:
            analyze_result_console.print("[dim]No parent classes found[/dim]")
        
        analyze_result_console.print()
        
        # Show child classes
        if results.get('child_classes'):
            analyze_result_console.print("[bold yellow]Children (classes that inherit from this):[/bold yellow]")
            for child in results['child_classes']:
                analyze_result_console.print(f"  ⬇ [cyan]{child.get('child_class', '')}[/cyan] [dim]({child.get('child_file_path', '')}:{child.get('child_line_number', '')})[/dim]")
        else:
            analyze_result_console.print("[dim]No child classes found[/dim]")
        
        analyze_result_console.print()
        
        # Show methods
        if results.get('methods'):
            analyze_result_console.print(f"[bold yellow]Methods ({len(results['methods'])}):[/bold yellow]")
            for method in results['methods'][:10]:  # Limit to 10
                analyze_result_console.print(f"  • [green]{method.get('method_name', '')}[/green]({method.get('method_args', '')})")
            if len(results['methods']) > 10:
                analyze_result_console.print(f"  [dim]... and {len(results['methods']) - 10} more[/dim]")
    finally:
        db_manager.close_driver()

@analyze_app.command("complexity")
def analyze_complexity(
    path: Optional[str] = typer.Argument(None, help="Function name or file path to analyze"),
    threshold: Optional[int] = typer.Option(None, "--threshold", "-t", help="Complexity threshold for warnings (default: from config or 10)"),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Maximum results to show (default 20 for text; unlimited for json/csv)"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Specific file path to scope analysis"),
    output_format: str = typer.Option("text", "--format", help="Output format: text, json or csv (#1333)"),
    fail_on_violations: bool = typer.Option(False, "--fail-on-violations", help="Exit with code 1 when any function exceeds the threshold (CI gate)"),
):
    """
    Show cyclomatic complexity for functions.
    
    Example:
        cviz analyze complexity                    # Most complex functions
        cviz analyze complexity --threshold 15     # Functions over threshold
        cviz analyze complexity my_function        # Specific function
        cviz analyze complexity my_function -f file.py # Specific function in file
        cviz analyze complexity src/main.py        # Most complex functions in file
        cviz analyze complexity main.py            # Most complex functions in file
        cviz analyze complexity --file src/main.py # Alternative file syntax
        cviz analyze complexity -t 10 --format json --fail-on-violations  # CI gate
    """
    output_format = output_format.lower()
    if output_format not in ("text", "json", "csv"):
        console.print(f"[red]Unknown --format '{output_format}' (expected text, json or csv)[/red]")
        raise typer.Exit(code=2)
    # stdout must carry ONLY the parseable document in machine formats: the
    # service-init chatter from cli_helpers prints to stdout, so reroute that
    # console to stderr FOR THE INIT WINDOW ONLY (`--format json | jq .`).
    # The rebind must be restored — a permanent one leaks a closed stream into
    # later in-process invocations (CliRunner-based tests).
    import sys as _sys
    from . import cli_helpers as _cli_helpers
    # NB: the .file property GETTER materializes the current (possibly
    # transient, CliRunner-captured) stream; the dynamic state lives in
    # ._file, which is None while the console follows sys.stdout live.
    _prev_console_file = _cli_helpers.console._file
    if output_format in ("json", "csv"):
        _cli_helpers.console.file = _sys.stderr
    try:
        _load_credentials()
        services = _initialize_services()
    finally:
        _cli_helpers.console._file = _prev_console_file
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]

    # Read threshold from config if not explicitly provided via CLI
    if threshold is None:
        configured = config_manager.get_config_value("COMPLEXITY_THRESHOLD")
        if configured is not None:
            try:
                threshold = int(configured)
            except (ValueError, TypeError):
                threshold = 10
        else:
            threshold = 10

    machine_output = output_format in ("json", "csv")
    # Enforcement and export need the full set: a display page must never
    # truncate the violation count a CI gate acts on (#1333).
    effective_limit = limit if limit is not None else (None if (machine_output or fail_on_violations) else 20)

    def _violations_from(results):
        rows = []
        for func in results or []:
            # The synthetic `<module>` frame is the attribution target for
            # module-level calls, not a function anyone can refactor.
            if func.get("function_name") == "<module>":
                continue
            complexity = func.get("complexity", 0) or 0
            if complexity > threshold:
                rows.append({
                    "function": func.get("function_name", ""),
                    "file": func.get("path", ""),
                    "line": func.get("line_number"),
                    "complexity": complexity,
                    "exceeds_by": complexity - threshold,
                })
        return rows

    def _emit_machine(violations):
        import csv as _csv
        import io as _io
        import json as _json
        if output_format == "json":
            print(_json.dumps({
                "threshold": threshold,
                "violations_count": len(violations),
                "violations": violations,
            }, indent=2))
        else:
            buf = _io.StringIO()
            w = _csv.writer(buf)
            w.writerow(["function", "file", "line", "complexity", "exceeds_by"])
            for v in violations:
                w.writerow([v["function"], v["file"], v["line"], v["complexity"], v["exceeds_by"]])
            print(buf.getvalue(), end="")

    _FILE_EXTENSIONS = ('.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.rb',
                        '.java', '.cpp', '.c', '.cs', '.swift', '.kt', '.scala',
                        '.php', '.lua', '.zig', '.ex', '.exs', '.r', '.m', '.sh')

    def _is_file_path(value: str) -> bool:
        if '/' in value or '\\' in value:
            return True
        return any(value.endswith(ext) for ext in _FILE_EXTENSIONS)

    def _render_complexity_table(results, title):
        if not results:
            analyze_result_console.print("[yellow]No complexity data available for this file[/yellow]")
            return
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Function", style="cyan")
        table.add_column("Complexity", style="yellow", justify="right")
        table.add_column("Location", style="dim", overflow="fold")
        for func in results:
            complexity = func.get('complexity', 0)
            color = "red" if complexity > threshold else "yellow" if complexity > threshold/2 else "green"
            fpath = func.get('path', '')
            line_str = str(func.get('line_number', ''))
            location_str = f"{fpath}:{line_str}" if line_str else fpath
            table.add_row(
                func.get('function_name', ''),
                f"[{color}]{complexity}[/{color}]",
                location_str
            )
        analyze_result_console.print(f"\n[bold cyan]{title}[/bold cyan]")
        analyze_result_console.print(table)
        analyze_result_console.print(f"\n[dim]{len([f for f in results if f.get('complexity', 0) > threshold])} function(s) exceed threshold[/dim]")

    violations = None
    try:
        if path and _is_file_path(path):
            # File path provided as positional argument
            results = code_finder.find_most_complex_functions_in_file(path, effective_limit)
            violations = _violations_from(results)
            if machine_output:
                _emit_machine(violations)
            else:
                _render_complexity_table(results, f"Most Complex Functions in '{path}' (threshold: {threshold}):")
        elif path:
            # Specific function name
            result = code_finder.get_cyclomatic_complexity(path, file)
            single = []
            if result:
                single = [{
                    "function_name": path,
                    "path": result.get("path", ""),
                    "line_number": result.get("line_number"),
                    "complexity": result.get("complexity", 0) or 0,
                }]
            violations = _violations_from(single)
            if machine_output:
                _emit_machine(violations)
            elif result:
                analyze_result_console.print(f"\n[bold cyan]Complexity for '{path}':[/bold cyan]")
                analyze_result_console.print(f"  Cyclomatic Complexity: [yellow]{result.get('complexity', 'N/A')}[/yellow]")
                analyze_result_console.print(f"  File: [dim]{result.get('path', '')}[/dim]")
                analyze_result_console.print(f"  Line: [dim]{result.get('line_number', '')}[/dim]")
            else:
                analyze_result_console.print(f"[yellow]Function '{path}' not found or has no complexity data[/yellow]")
        elif file:
            # --file option without positional arg
            results = code_finder.find_most_complex_functions_in_file(file, effective_limit)
            violations = _violations_from(results)
            if machine_output:
                _emit_machine(violations)
            else:
                _render_complexity_table(results, f"Most Complex Functions in '{file}' (threshold: {threshold}):")
        else:
            # Global - most complex functions
            results = code_finder.find_most_complex_functions(effective_limit)
            violations = _violations_from(results)
            if machine_output:
                _emit_machine(violations)
            else:
                _render_complexity_table(results, f"Most Complex Functions (threshold: {threshold}):")
    finally:
        db_manager.close_driver()

    # CI gate (#1333): explicit opt-in keeps exploratory usage exit-0 —
    # the config default threshold means most codebases have *some*
    # function above it, and failing every casual invocation would break
    # innocent `cviz analyze complexity && …` chains.
    if fail_on_violations and violations:
        raise typer.Exit(code=1)

@analyze_app.command("dead-code")
def analyze_dead_code(
    path: Optional[str] = typer.Argument(None, help="Repository path to scope the analysis to"),
    exclude_decorators: Optional[str] = typer.Option(None, "--exclude", "-e", help="Comma-separated decorators to exclude"),
    show_all: bool = typer.Option(False, "--show-all", help="Include low-confidence rows (dunders, test hooks, entry-point names) normally hidden (#1332)"),
):
    """
    Find potentially unused functions.

    Without a path the whole database is scanned, which on a shared graph
    reports dead code from every indexed repository. Pass a path to scope it.

    Example:
        cviz analyze dead-code .
        cviz analyze dead-code --exclude route,task,api
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        exclude_list = exclude_decorators.split(',') if exclude_decorators else []
        # `path` was accepted and then dropped, so running inside one repository
        # still reported dead code from every repository in the database.
        repo_path = Path(path).resolve().as_posix() if path else None
        # find_dead_code used to cap itself at 50 rows inside the query, which
        # doubled as this table's page size by accident. Now that it returns
        # the full set, ask for a page explicitly -- otherwise a real codebase
        # (7,141 dead functions was the reported figure) would print thousands
        # of table rows. The true count comes from total_count below (#1606).
        display_limit = get_tool_result_limit("find_dead_code")
        results = code_finder.find_dead_code(
            exclude_list, repo_path=repo_path, limit=display_limit,
            include_low_confidence=show_all,
        )

        unused_funcs = results.get('potentially_unused_functions', [])
        total_count = results.get('total_count', len(unused_funcs))

        if not unused_funcs:
            analyze_result_console.print("[green]✓ No dead code found![/green]")
            return
        
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Function", style="cyan")
        table.add_column("Confidence", justify="center")
        table.add_column("Location", style="dim", overflow="fold")

        _conf_render = {
            "high": "[red]● high[/red]",
            "medium": "[yellow]● medium[/yellow]",
            "low": "[dim]● low[/dim]",
        }
        for func in unused_funcs:
            fpath = func.get('path', '')
            line_str = str(func.get('line_number', ''))
            location_str = f"{fpath}:{line_str}" if line_str else fpath
            table.add_row(
                func.get('function_name', ''),
                _conf_render.get(func.get('confidence', 'high'), func.get('confidence', '')),
                location_str,
            )
        
        analyze_result_console.print("\n[bold yellow]⚠️  Potentially Unused Functions:[/bold yellow]")
        analyze_result_console.print(table)
        if total_count > len(unused_funcs):
            analyze_result_console.print(
                f"\n[dim]Total: {total_count} function(s); "
                f"showing the first {len(unused_funcs)} by path[/dim]"
            )
        else:
            analyze_result_console.print(f"\n[dim]Total: {total_count} function(s)[/dim]")
        counts = results.get('confidence_counts') or {}
        if counts:
            summary = (
                f"[red]{counts.get('high', 0)} high[/red], "
                f"[yellow]{counts.get('medium', 0)} medium[/yellow], "
                f"[dim]{counts.get('low', 0)} low[/dim] confidence"
            )
            if not show_all:
                summary += " [dim](low-confidence categories are hidden — use --show-all)[/dim]"
            analyze_result_console.print(summary)
        analyze_result_console.print(f"[dim]Note: {results.get('note', '')}[/dim]")
    finally:
        db_manager.close_driver()

@analyze_app.command("overrides")
def analyze_overrides(
    ctx: typer.Context,
    function_name: str = typer.Argument(..., help="Function/method name to find implementations of"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Find all implementations of a function across different classes.
    
    Useful for finding polymorphic implementations and method overrides.
    
    Example:
        cviz analyze overrides area
        cviz analyze overrides process
        cviz analyze overrides area --visual
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        req_limit = get_tool_result_limit("overrides")
        results = code_finder.find_function_overrides(function_name, limit=req_limit + 1 if req_limit is not None else None)
        truncated = bool(req_limit and len(results) > req_limit)
        if truncated:
            results = results[:req_limit]
        
        if not results:
            analyze_result_console.print(f"[yellow]No implementations found for function '{function_name}'[/yellow]")
            return
        
        # Check if visual mode is enabled
        if check_visual_flag(ctx, visual):
            visualize_overrides(results, function_name)
            return
        
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Class", style="cyan")
        table.add_column("Function", style="green")
        table.add_column("Location", style="dim", overflow="fold")
        
        for res in results:
            path = res.get('class_file_path', '')
            line_str = str(res.get('function_line_number', ''))
            location_str = f"{path}:{line_str}" if line_str else path

            table.add_row(
                res.get('class_name', ''),
                res.get('function_name', ''),
                location_str
            )
        
        analyze_result_console.print(f"\n[bold cyan]Found {len(results)} implementation(s) of '{function_name}':[/bold cyan]")
        analyze_result_console.print(table)
        if truncated:
            analyze_result_console.print(f"[dim]... truncated ({req_limit} shown), more exist[/dim]")
    finally:
        db_manager.close_driver()

@analyze_app.command("variable")
def analyze_variable_usage(
    variable_name: str = typer.Argument(..., help="Variable name to analyze"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Specific file path"),
):
    """
    Analyze where a variable is defined and used across the codebase.
    
    Shows all instances of the variable and their scope (function, class, module).
    
    Example:
        cviz analyze variable MAX_RETRIES
        cviz analyze variable config
        cviz analyze variable x --file math_utils.py
    """
    _load_credentials()
    services = _initialize_services()
    if not all(services[:3]):
        raise typer.Exit(code=1)
    db_manager, graph_builder, code_finder = services[:3]
    
    try:
        # Get variable usage scope
        scope_results = code_finder.find_variable_usage_scope(variable_name, file)
        instances = scope_results.get('instances', [])
        
        if not instances:
            analyze_result_console.print(f"[yellow]No instances found for variable '{variable_name}'[/yellow]")
            return
        
        analyze_result_console.print(f"\n[bold cyan]Variable '{variable_name}' Usage Analysis:[/bold cyan]\n")
        
        # Group by scope type
        by_scope = {}
        for inst in instances:
            scope_type = inst.get('scope_type', 'unknown')
            if scope_type not in by_scope:
                by_scope[scope_type] = []
            by_scope[scope_type].append(inst)
        
        # Display by scope
        for scope_type, items in by_scope.items():
            analyze_result_console.print(f"[bold yellow]{scope_type.upper()} Scope ({len(items)} instance(s)):[/bold yellow]")
            
            table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
            table.add_column("Scope Name", style="cyan")
            table.add_column("Location", style="dim", overflow="fold")
            table.add_column("Value", style="yellow")
            
            for item in items:
                path = item.get('path', '')
                line_str = str(item.get('line_number', ''))
                location_str = f"{path}:{line_str}" if line_str else path

                table.add_row(
                    item.get('scope_name', ''),
                    location_str,
                    str(item.get('variable_value', ''))[:50] if item.get('variable_value') else '-'
                )
            
            analyze_result_console.print(table)
            analyze_result_console.print()
        
        analyze_result_console.print(f"[dim]Total: {len(instances)} instance(s) across {len(by_scope)} scope type(s)[/dim]")
    finally:
        db_manager.close_driver()


# ============================================================================
# QUERY COMMAND - Raw Cypher Queries
# ============================================================================

@app.command("query")
def query_graph(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Cypher query to execute (read-only)"),
    visual: bool = typer.Option(False, "--visual", "--viz", "-V", help="Show results as interactive graph visualization"),
):
    """
    Execute a custom Cypher query on the code graph.
    
    Examples:
        cviz query "MATCH (f:Function) RETURN f.name LIMIT 10"
        cviz query "MATCH (c:Class)-[:CONTAINS]->(m) RETURN c.name, count(m)"
        cviz query "MATCH (n)-[r]->(m) RETURN n,r,m LIMIT 50" --visual
    """
    _load_credentials()
    
    # Check if visual mode is enabled
    if check_visual_flag(ctx, visual):
        cypher_helper_visual(query, None)
    else:
        cypher_helper(query, None)



# ============================================================================
# ABBREVIATIONS / SHORTCUTS for common commands
# ============================================================================

@app.command("i", rich_help_panel="Shortcuts")
def index_abbrev(
    path: Optional[str] = typer.Argument(None, help="Path to index"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-index (delete existing and rebuild)"),
    summarize: bool = typer.Option(False, "--summarize", "-s", help="Display a codebase summary after indexing"),
    no_progress: bool = typer.Option(False, "--no-progress", help="Disable live progress rendering during indexing."),
    embed: bool = typer.Option(False, "--embed", "-e", help="Also vectorize for the natural-language search."),
):
    """Shortcut for 'cviz index'"""
    # `summarize` must be passed explicitly: omitted, it keeps its OptionInfo
    # sentinel, which is truthy — so `cviz i` always printed the summary.
    index(path, force=force, summarize=summarize, no_progress=no_progress, embed=embed)

@app.command("ls", rich_help_panel="Shortcuts")
def list_abbrev(
):
    """Shortcut for 'cviz list'"""
    list_repositories()

@app.command("rm", rich_help_panel="Shortcuts")
def delete_abbrev(
    path: Optional[str] = typer.Argument(None, help="Path to delete"),
    all_repos: bool = typer.Option(False, "--all", help="Delete all indexed repositories"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt (for CI/non-interactive use)"),
):
    """Shortcut for 'cviz delete'"""
    # `yes` must be forwarded explicitly: omitted, it keeps its OptionInfo
    # sentinel, which is truthy — silently skipping the confirmation.
    delete(path, all_repos, yes=yes)

@app.command("v", rich_help_panel="Shortcuts")
def visualize_abbrev(
    repo: Optional[str] = typer.Argument(None, help="Path to the repository to visualize."),
    port: int = typer.Option(DEFAULT_GRAPH_PORT, "--port", "-p", help="Port to run the visualizer server on."),
):
    """Shortcut for 'cviz visualize'"""
    _load_credentials()
    # `port` must be passed by keyword: visualize_helper's second positional
    # parameter is `host`, so the int landed there and the server tried to bind
    # to host=8000 while ignoring --port entirely.
    visualize_helper(repo, port=port)

@app.command("w", rich_help_panel="Shortcuts")
def watch_abbrev(
    path: str = typer.Argument(".", help="Path to watch"),
    poll: bool = typer.Option(
        False,
        "--poll",
        help="Use watchdog's polling observer for Docker bind mounts and network filesystems.",
    ),
    sync_on_start: bool = typer.Option(
        False,
        "--sync-on-start",
        help=(
            "Re-parse every already-indexed file before watching (full reconcile). "
            "Defaults off; use 'cviz index --force' for a full re-index."
        ),
    ),
    catch_up: bool = typer.Option(
        True,
        "--catch-up/--no-catch-up",
        help=(
            "Before watching, update only the files added, changed or deleted "
            "since the last update (cheap; on by default)."
        ),
    ),
    full_resync_hours: float = typer.Option(
        0,
        "--full-resync-hours",
        help="Also re-synchronise the whole repository every N hours (0 = never).",
    ),
):
    """Shortcut for 'cviz watch'"""
    watch(
        path,
        poll=poll,
        sync_on_start=sync_on_start,
        catch_up=catch_up,
        full_resync_hours=full_resync_hours,
    )


# ============================================================================



@app.command()
def help(ctx: typer.Context):
    """Show the main help message and exit."""
    root_ctx = ctx.parent or ctx
    typer.echo(root_ctx.get_help())


@app.command("version")
def version_cmd():
    """Show the application version."""
    console.print(f"Code Visualizer [bold cyan]{get_version()}[/bold cyan]")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    database: Optional[str] = typer.Option(
        None, 
        "--database", 
        "--db",
        "-db", 
        help="[Global] Temporarily override database backend ("
        + "|".join(config_manager.SUPPORTED_DATABASES)
        + ") for any command"
    ),
    visual: bool = typer.Option(
        False,
        "--visual",
        "--viz",
        "-V",
        help="[Global] Show results as interactive graph visualization in browser"
    ),
    version_: bool = typer.Option(
        None,
        "--version",
        "-v",
        help="[Root-level only] Show version and exit",
        is_eager=True,
    ),
    help_: bool = typer.Option(
        None,
        "--help",
        "-h",
        help="[Root-level only] Show help and exit",
        is_eager=True,
    ),
    db_path: Optional[str] = typer.Option(
        None,
        "--path",
        "--db-path",
        help="[Global] Temporarily override database path (LadybugDB directory)"
    ),
):
    """
    Main entry point for the cviz CLI application.
    If no subcommand is provided, it displays a welcome message with instructions.
    """
    if db_path:
        os.environ["CVIZ_RUNTIME_DB_PATH"] = db_path
    if database:
        os.environ["CVIZ_RUNTIME_DB_TYPE"] = database
    # Initialize context object for sharing state with subcommands
    ctx.ensure_object(dict)

    # Store visual flag in context for subcommands to access
    if visual:
        ctx.obj["visual"] = True

    if version_:
        console.print(f"Code Visualizer [bold cyan]{get_version()}[/bold cyan]")
        raise typer.Exit()
    
    if help_:
        typer.echo(ctx.get_help())
        raise typer.Exit()

    if ctx.invoked_subcommand is None:
        console.print("[bold green]👋 Welcome to Code Visualizer (cviz)![/bold green]\n")
        console.print("Code Visualizer is both an [bold cyan]MCP server[/bold cyan] and a [bold cyan]CLI toolkit[/bold cyan] for code analysis.\n")
        console.print("🤖 [bold]For MCP Server Mode (AI assistants):[/bold]")
        console.print("   1. Run [cyan]cviz mcp setup[/cyan] (or [cyan]cviz m[/cyan]) to configure your IDE")
        console.print("   2. Run [cyan]cviz mcp start[/cyan] to launch the server\n")
        console.print("🛠️  [bold]For CLI Toolkit Mode (direct usage):[/bold]")
        console.print("   • [cyan]cviz index .[/cyan] - Index your current directory")
        console.print("   • [cyan]cviz list[/cyan] - List indexed repositories\n")
        console.print("📈 [bold]Want visual graph output?[/bold]")
        console.print("     Add [cyan]-V[/cyan] or [cyan]--visual[/cyan] to any analyze/find command\n")
        console.print("👉 Run [cyan]cviz help[/cyan] to see all available commands")
        console.print("👉 Run [cyan]cviz --version[/cyan] to check the version\n")
        console.print("👉 Running [green]code_visualizer[/green] works the same as using [green]cviz[/green]")


# ============================================================================
# DATASOURCE COMMAND GROUP — Index external data sources (#843)
# ============================================================================

datasource_app = typer.Typer(help="Index external data sources (Redis, Cassandra, Aurora MySQL) into the code graph")
app.add_typer(datasource_app, name="datasource")


@datasource_app.command("mysql")
def datasource_mysql(
    ctx: typer.Context,
    host: str = typer.Option(..., "--host", "-H", help="MySQL host / Aurora endpoint"),
    port: int = typer.Option(3306, "--port", "-p", help="MySQL port"),
    user: str = typer.Option(..., "--user", "-u", help="MySQL username"),
    password: str = typer.Option(..., "--password", "-P", help="MySQL password", hide_input=True),
    database: str = typer.Option(..., "--database", "-d", help="Database / schema name"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Logical datasource name (default: mysql-<database>)"),
    env: str = typer.Option("production", "--env", "-e", help="Deployment environment label"),
    ssl_verify: bool = typer.Option(False, "--ssl-verify", help="Connect over TLS and verify the server certificate (#1094)"),
    ssl_ca_certs: Optional[str] = typer.Option(None, "--ssl-ca-certs", help="CA bundle path for --ssl-verify (default: system trust store)"),
):
    """Ingest Aurora MySQL schema (tables + columns) and write to the code graph.

    Requires: pip install PyMySQL
    """
    try:
        from code_visualizer.tools.datasources.mysql_ingester import ingest as mysql_ingest
    except ImportError as e:
        console.print(f"[red]Import error:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"[cyan]Connecting to Aurora MySQL at {host}:{port}/{database}...[/cyan]")
    try:
        result = mysql_ingest(host=host, port=port, user=user, password=password,
                               database=database, name=name, env=env,
                               ssl_verify=ssl_verify, ssl_ca_certs=ssl_ca_certs)
    except Exception as exc:
        console.print(f"[red]Failed to connect / ingest:[/red] {exc}")
        raise typer.Exit(1)

    _write_datasource_graph(result)
    console.print(
        f"[green]✓ MySQL datasource[/green] [bold]{result['datasource']['name']}[/bold] indexed: "
        f"{len(result.get('tables', []))} tables, {len(result.get('columns', []))} columns"
    )


@datasource_app.command("cassandra")
def datasource_cassandra(
    ctx: typer.Context,
    host: str = typer.Option(..., "--host", "-H", help="Cassandra contact point (comma-separated for multiple)"),
    port: int = typer.Option(9042, "--port", "-p", help="Cassandra native transport port"),
    keyspace: str = typer.Option(..., "--keyspace", "-k", help="Keyspace to ingest"),
    username: Optional[str] = typer.Option(None, "--user", "-u", help="Cassandra username"),
    password: Optional[str] = typer.Option(None, "--password", "-P", help="Cassandra password", hide_input=True),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Logical datasource name (default: cassandra-<keyspace>)"),
    env: str = typer.Option("production", "--env", "-e", help="Deployment environment label"),
    ssl_verify: bool = typer.Option(False, "--ssl-verify", help="Connect over TLS and verify the server certificate (#1094)"),
    ssl_ca_certs: Optional[str] = typer.Option(None, "--ssl-ca-certs", help="CA bundle path for --ssl-verify (default: system trust store)"),
):
    """Ingest Cassandra keyspace schema (tables + columns) and write to the code graph.

    Requires: pip install cassandra-driver
    """
    try:
        from code_visualizer.tools.datasources.cassandra_ingester import ingest as cassandra_ingest
    except ImportError as e:
        console.print(f"[red]Import error:[/red] {e}")
        raise typer.Exit(1)

    hosts = [h.strip() for h in host.split(",")]
    console.print(f"[cyan]Connecting to Cassandra at {hosts}/{keyspace}...[/cyan]")
    try:
        result = cassandra_ingest(hosts=hosts, port=port, keyspace=keyspace,
                                   username=username, password=password, name=name, env=env,
                                   ssl_verify=ssl_verify, ssl_ca_certs=ssl_ca_certs)
    except Exception as exc:
        console.print(f"[red]Failed to connect / ingest:[/red] {exc}")
        raise typer.Exit(1)

    _write_datasource_graph(result)
    console.print(
        f"[green]✓ Cassandra datasource[/green] [bold]{result['datasource']['name']}[/bold] indexed: "
        f"{len(result.get('tables', []))} tables, {len(result.get('columns', []))} columns"
    )


@datasource_app.command("redis")
def datasource_redis(
    ctx: typer.Context,
    host: str = typer.Option(..., "--host", "-H", help="Redis host"),
    port: int = typer.Option(6379, "--port", "-p", help="Redis port"),
    db: int = typer.Option(0, "--db", help="Redis database index"),
    password: Optional[str] = typer.Option(None, "--password", "-P", help="Redis AUTH password", hide_input=True),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Logical datasource name"),
    env: str = typer.Option("production", "--env", "-e", help="Deployment environment label"),
    max_keys: int = typer.Option(10000, "--max-keys", help="Maximum keys to scan (avoid full scan in large clusters)"),
):
    """Discover Redis key patterns and write to the code graph.

    Scans up to --max-keys keys and groups them into patterns (e.g. user:*).

    Requires: pip install redis
    """
    try:
        from code_visualizer.tools.datasources.redis_ingester import ingest as redis_ingest
    except ImportError as e:
        console.print(f"[red]Import error:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"[cyan]Connecting to Redis at {host}:{port}/{db}...[/cyan]")
    try:
        result = redis_ingest(host=host, port=port, db=db, password=password,
                               name=name, env=env, max_keys=max_keys)
    except Exception as exc:
        console.print(f"[red]Failed to connect / ingest:[/red] {exc}")
        raise typer.Exit(1)

    _write_datasource_graph(result)
    console.print(
        f"[green]✓ Redis datasource[/green] [bold]{result['datasource']['name']}[/bold] indexed: "
        f"{len(result.get('key_patterns', []))} key patterns"
    )


def _write_datasource_graph(ingested: dict, context: Optional[str] = None) -> None:
    """Shared helper: write ingested datasource dict to the active graph."""
    from .cli_helpers import _initialize_services, _fail_services_init

    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, _, _ctx = services
    try:
        driver = db_manager.get_driver()
        if driver is None:
            console.print("[red]No active graph connection.[/red]")
            raise typer.Exit(1)

        from code_visualizer.tools.indexing.persistence.writer import GraphWriter
        GraphWriter(driver, db_manager=db_manager).write_datasource_graph(ingested)
    finally:
        db_manager.close_driver()


from code_visualizer.cli.simulator import simulate_app
app.add_typer(simulate_app, name="simulate")


if __name__ == "__main__":
    app()
