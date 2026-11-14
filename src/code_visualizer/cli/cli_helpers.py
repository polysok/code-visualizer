# src/code_visualizer/cli/cli_helpers.py
import asyncio
import json
import uuid
import urllib.error
import urllib.parse
from collections import Counter
from pathlib import Path
import time
import os
from typing import Optional, List, Dict, Any
from .open_in_graph import DEFAULT_PORT as DEFAULT_GRAPH_PORT
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
    MofNCompleteColumn,
)

from ..core import get_database_manager
from ..core.jobs import JobManager
from ..tools.code_finder import CodeFinder
from ..tools.graph_builder import GraphBuilder
from ..utils import graph_shapes
from ..tools.package_resolver import get_local_package_path
from ..utils.debug_log import info_logger, warning_logger
from ..utils.repo_path import any_repo_matches_path
from .config_manager import (
    resolve_context,
    ResolvedContext,
    ensure_first_run_bootstrap,
    NotInAProjectError,
    is_db_deletion_allowed,
    get_config_value,
)

console = Console()


def _fail_services_init() -> None:
    """Abort the CLI command when database/services could not be initialized."""
    raise typer.Exit(code=1)


def _print_call_resolution_diagnostics(graph_builder: GraphBuilder, limit: int = 5) -> None:
    diagnostics = getattr(graph_builder, "last_call_resolution_diagnostics", [])
    if not diagnostics:
        return

    reason_counts = Counter(d.get("reason", "unknown") for d in diagnostics)
    summary = ", ".join(
        f"{reason}={count}" for reason, count in reason_counts.most_common()
    )
    console.print(
        f"[yellow]Skipped {len(diagnostics)} unresolved call relationship(s): {summary}[/yellow]"
    )
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Call", style="cyan", overflow="fold")
    table.add_column("Reason", style="yellow")
    table.add_column("Location", style="dim", overflow="fold")
    for diagnostic in diagnostics[:limit]:
        table.add_row(
            str(diagnostic.get("full_call_name") or ""),
            str(diagnostic.get("reason") or ""),
            f"{diagnostic.get('caller_file_path')}:{diagnostic.get('line_number')}",
        )
    console.print(table)


def _format_extension_counts(files_by_extension: Dict[str, int]) -> str:
    if not files_by_extension:
        return "None"
    return ", ".join(
        f"{extension}: {count}" for extension, count in files_by_extension.items()
    )


def _print_index_execution_summary(graph_builder: GraphBuilder) -> None:
    summary = getattr(graph_builder, "last_index_summary", None)
    if not summary:
        return

    table = Table(
        title="CViz Index Execution Summary",
        show_header=True,
        header_style="bold cyan",
        box=box.ASCII,
    )
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="green", overflow="fold")
    table.add_row("Total scanned files", str(summary.get("total_scanned_files", 0)))
    table.add_row(
        "Files by extension",
        _format_extension_counts(summary.get("files_by_extension", {})),
    )
    failed_files = summary.get("failed_files", 0)
    if failed_files:
        table.add_row("[red]Files failed to parse[/red]", f"[red]{failed_files}[/red]")
    table.add_row("Function nodes", str(summary.get("function_nodes", 0)))
    table.add_row("Class nodes", str(summary.get("class_nodes", 0)))
    table.add_row("CALLS edges", str(summary.get("call_edges", 0)))
    table.add_row(
        "Serialization seconds",
        f"{summary.get('serialization_seconds', 0.0):.2f}",
    )
    console.print(table)
    # Always-visible warnings (#1597): these describe an explicitly enabled
    # feature that did NOT run — they must not depend on the log level.
    for warning in summary.get("warnings", []):
        console.print(f"[bold yellow]⚠ {warning}[/bold yellow]")


def _initialize_services(
    cli_context_flag: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> tuple[Any, Any, Any, ResolvedContext]:
    """
    Initializes and returns core service managers based on the resolved context.
    Returns (db_manager, graph_builder, code_finder, resolved_context).
    """
    ensure_first_run_bootstrap()
    try:
        ctx = resolve_context(cwd=cwd)
    except NotInAProjectError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[dim]Repository: {getattr(ctx, 'repo_root', '') or ctx.db_path}[/dim]")

    console.print("[dim]Initializing services and database connection...[/dim]")
    try:
        # Respect runtime/backend overrides. Context DB is only a default when
        # neither runtime override nor DEFAULT_DATABASE is already set.
        if (
            ctx.database
            and not os.getenv("CVIZ_RUNTIME_DB_TYPE")
            and not os.getenv("DEFAULT_DATABASE")
        ):
            os.environ["DEFAULT_DATABASE"] = ctx.database
        
        # Pass the exact DB path resolved from the context, or the runtime override
        runtime_path = os.getenv("CVIZ_RUNTIME_DB_PATH")
        db_manager = get_database_manager(db_path=runtime_path or ctx.db_path)
    except ValueError as e:
        console.print(f"[bold red]Database Configuration Error:[/bold red] {e}")
        _fail_services_init()

    try:
        db_manager.get_driver()
    except Exception as e:
        console.print(f"[bold red]Database Connection Error:[/bold red] {e}")
        if "Could not set lock on file" in str(e):
            console.print(
                "[yellow]Another CViz process already has the LadybugDB database open "
                "(it accepts a single process). Stop it, or use the page / watcher "
                "started by 'cviz graph --watch' instead of a separate command.[/yellow]"
            )
        else:
            console.print("Please check the database path or run 'cviz doctor'.")
        _fail_services_init()
    
    
    # The GraphBuilder requires an event loop, even for synchronous-style execution
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    graph_builder = GraphBuilder(db_manager, JobManager(), loop)
    code_finder = CodeFinder(db_manager)
    console.print("[dim]Services initialized.[/dim]")
    return db_manager, graph_builder, code_finder, ctx


async def _run_index_with_progress(graph_builder: GraphBuilder, path_obj: Path, is_dependency: bool = False, cvizignore_path: str = None, disable_progress: bool = False):
    """Internal helper to run indexing with a Live progress bar."""
    job_id = graph_builder.job_manager.create_job(str(path_obj), is_dependency=is_dependency)
    
    disable_progress = (
        disable_progress
        or not console.is_terminal
        or os.environ.get("CI", "").lower() == "true"
    )

    if not disable_progress:
        os.environ["CVIZ_ACTIVE_PROGRESS_BAR"] = "1"

    try:
        # Create the progress bar
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            TextColumn("[dim]{task.fields[filename]}"),
            console=console,
            transient=True,
            disable=disable_progress,
        ) as progress:
        
            task_id = progress.add_task(
                "Indexing...", 
                total=None,  # Will be updated once file discovery is done
                filename=""
            )

            indexing_task = asyncio.create_task(
                graph_builder.build_graph_from_path_async(path_obj, is_dependency=is_dependency, job_id=job_id, cvizignore_path=cvizignore_path)
            )

            from ..core.jobs import JobStatus
            
            # Poll for updates
            while not indexing_task.done():
                job = graph_builder.job_manager.get_job(job_id)
                if job:
                    if job.total_files > 0:
                        progress.update(task_id, total=job.total_files, completed=job.processed_files)
                    
                    # Prefer post-processing status over the last parsed file path
                    current_file = job.status_message or job.current_file or ""
                    if len(current_file) > 40:
                        current_file = "..." + current_file[-37:]
                    progress.update(task_id, filename=current_file)

                    if job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
                        break
                
                await asyncio.sleep(0.1)

            # Wait for actual completion and handle final state
            try:
                await indexing_task
                job = graph_builder.job_manager.get_job(job_id)
                if job and job.status == JobStatus.FAILED:
                    error_msg = job.errors[0] if job.errors else "Unknown error"
                    raise RuntimeError(error_msg)
            except Exception as e:
                raise e
    finally:
        os.environ.pop("CVIZ_ACTIVE_PROGRESS_BAR", None)


def _print_deferred_vectorization_tip(embed: Optional[bool]) -> None:
    """Say where the vectors come from when the index left them out."""
    if embed is not False:
        return
    try:
        from ..semantic import settings as ai_settings
        if not ai_settings.enabled():
            return
    except Exception:  # noqa: BLE001
        return
    console.print("[dim]Vectorization deferred: 'cviz graph' runs it at start-up "
                  "(or now: 'cviz embed', or 'cviz index --embed').[/dim]")


def _print_companions(path_obj: Path) -> None:
    from ..core import companions

    roots = companions.optional_roots(path_obj) if path_obj.is_dir() else []
    if roots:
        console.print(f"[dim]With {len(roots)} companion source(s): "
                      + ", ".join(r.name for r in roots) + "[/dim]")


def index_helper(path: str, context: Optional[str] = None, no_progress: bool = False,
                 embed: Optional[bool] = None):
    """Synchronously indexes a repository in a given context."""
    time_start = time.time()
    path_obj = Path(path).resolve()
    # Normalize to forward slashes for cross-platform DB consistency.
    # The graph DB always stores paths via Path.resolve().as_posix(),
    # so Cypher queries must also use forward slashes on Windows.
    repo_path_str = path_obj.as_posix()
    index_cwd = path_obj if path_obj.is_dir() else path_obj.parent
    services = _initialize_services(context, cwd=index_cwd)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, code_finder, ctx = services
    graph_builder.semantic_search = embed

    if not path_obj.exists():
        console.print(f"[red]Error: Path does not exist: {path_obj}[/red]")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    indexed_repos = code_finder.list_indexed_repositories()
    repo_exists = any_repo_matches_path(indexed_repos, path_obj)

    if repo_exists:
        # Check if the repository actually has files (not just an empty node from interrupted indexing)
        # Use variable-length path to handle both flat (Repository->File) and
        # hierarchical (Repository->Directory->...->File) graph structures
        try:
            with db_manager.get_driver().session() as session:
                result = session.run(
                    "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(f:File) RETURN count(DISTINCT f) as file_count",
                    path=repo_path_str
                )
                record = result.single()
                file_count = record["file_count"] if record else 0
                
                if file_count > 0:
                    expected = graph_builder.estimate_processing_time(path_obj, cvizignore_path=ctx.cvizignore_path) if path_obj.is_dir() else None
                    expected_file_count = expected[0] if expected else None
                    if expected_file_count is None or file_count >= expected_file_count:
                        console.print(f"[yellow]Repository '{path}' is already indexed with {file_count} files. Skipping.[/yellow]")
                        console.print("[dim]💡 Tip: Use 'cviz index --force' to re-index[/dim]")
                        db_manager.close_driver()
                        return
                    console.print(
                        f"[yellow]Repository '{path}' has only {file_count} of {expected_file_count} files indexed. Continuing.[/yellow]"
                    )
                    # Name the gap: without this, a pipeline that can never
                    # produce nodes for some files loops on the message above
                    # forever with nothing actionable (#1673).
                    try:
                        from code_visualizer.tools.indexing.discovery import discover_files_to_index
                        discovered, _ = discover_files_to_index(
                            path_obj,
                            cvizignore_path=ctx.cvizignore_path,
                            supported_extensions=set(graph_builder.parsers.keys()),
                        )
                        with db_manager.get_driver().session() as s2:
                            res2 = s2.run(
                                "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(f:File) RETURN DISTINCT f.path AS p",
                                path=repo_path_str,
                            )
                            have = {row["p"] for row in res2 if row.get("p")}
                        missing = [
                            str(f) for f in discovered
                            if str(f.resolve()) not in have and f.resolve().as_posix() not in have
                        ]
                        if missing:
                            shown = ", ".join(Path(m).name for m in missing[:5])
                            more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
                            console.print(f"[dim]Missing: {shown}{more}[/dim]")
                    except Exception:
                        pass
                else:
                    console.print(f"[yellow]Repository '{path}' exists but has no files (likely interrupted). Re-indexing...[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not check file count: {e}. Proceeding with indexing...[/yellow]")

    console.print(f"Starting indexing for: {path_obj}")
    _print_companions(path_obj)

    try:
        asyncio.run(_run_index_with_progress(graph_builder, path_obj, is_dependency=False, cvizignore_path=ctx.cvizignore_path, disable_progress=no_progress))
        time_end = time.time()
        elapsed = time_end - time_start
        _print_call_resolution_diagnostics(graph_builder)
        _print_index_execution_summary(graph_builder)
        console.print(f"[green]Successfully finished indexing: {path_obj} in {elapsed:.2f} seconds[/green]")
        _print_deferred_vectorization_tip(embed)
        
        # Check if auto-watch is enabled
        try:
            from code_visualizer.cli.config_manager import get_config_value
            auto_watch = get_config_value('ENABLE_AUTO_WATCH')
            if auto_watch and str(auto_watch).lower() == 'true':
                console.print("\n[cyan]🔍 ENABLE_AUTO_WATCH is enabled. Starting watcher...[/cyan]")
                db_manager.close_driver()  # Close before starting watcher
                watch_helper(path)  # This will block the terminal
                return  # watch_helper handles its own cleanup
        except Exception as e:
            console.print(f"[yellow]Warning: Could not check ENABLE_AUTO_WATCH: {e}[/yellow]")
            
    except Exception as e:
        console.print(f"[bold red]An error occurred during indexing:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def add_package_helper(package_name: str, language: str, context: Optional[str] = None):
    """Synchronously indexes a package."""
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, code_finder, ctx = services

    package_path_str = get_local_package_path(package_name, language)
    if not package_path_str:
        console.print(f"[red]Error: Could not find package '{package_name}' for language '{language}'.[/red]")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    package_path = Path(package_path_str)
    
    indexed_repos = code_finder.list_indexed_repositories()
    if any(repo.get("name") == package_name for repo in indexed_repos if repo.get("is_dependency")):
        console.print(f"[yellow]Package '{package_name}' is already indexed. Skipping.[/yellow]")
        db_manager.close_driver()
        return

    console.print(f"Starting indexing for package '{package_name}' at: {package_path}")

    try:
        asyncio.run(_run_index_with_progress(graph_builder, package_path, is_dependency=True, cvizignore_path=ctx.cvizignore_path))
        _print_call_resolution_diagnostics(graph_builder)
        _print_index_execution_summary(graph_builder)
        console.print(f"[green]Successfully finished indexing package: {package_name}[/green]")
    except Exception as e:
        console.print(f"[bold red]An error occurred during package indexing:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def list_repos_helper(context: Optional[str] = None):
    """Lists all indexed repositories."""
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()
    
    db_manager, _, code_finder, ctx = services
    
    try:
        repos = code_finder.list_indexed_repositories()
        if not repos:
            console.print("[yellow]No repositories indexed yet.[/yellow]")
            return

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Name", style="dim")
        table.add_column("Path")
        table.add_column("Type")

        for repo in repos:
            repo_type = "Dependency" if repo.get("is_dependency") else "Project"
            table.add_row(repo.get("name") or "", str(repo.get("path") or ""), repo_type)
        
        console.print(table)
    except Exception as e:
        console.print(f"[bold red]An error occurred:[/bold red] {e}")
    finally:
        db_manager.close_driver()


def delete_helper(repo_path: str, context: Optional[str] = None):
    """Deletes a repository from the graph."""
    services = _initialize_services(context, cwd=Path(repo_path).resolve())
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, _, ctx = services
    
    try:
        if graph_builder.delete_repository_from_graph(repo_path):
            console.print(f"[green]Successfully deleted repository: {repo_path}[/green]")
        else:
            console.print(f"[yellow]Repository not found in graph: {repo_path}[/yellow]")
            console.print("[dim]Tip: Use 'cviz list' to see available repositories.[/dim]")
            raise typer.Exit(code=1)
    except typer.Exit:
        # Control flow, not an error — let it through so the exit code survives.
        raise
    except Exception as e:
        console.print(f"[bold red]An error occurred:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()

def _print_query_exception(e: Exception, query: str) -> None:
    """
    Pretty-print a database query exception, surfacing the raw driver
    error message so Cypher syntax problems are clearly visible.
    """
    import traceback

    error_type = type(e).__name__
    error_module = type(e).__module__ or ""

    # LadybugDB reports parser / binder errors as RuntimeError.
    if error_type == "RuntimeError" or "ladybug" in error_module:
        console.print(f"[bold red]Query Error ({error_type}):[/bold red]")
        console.print(f"  [yellow]Database message:[/yellow] {e}")
    else:
        console.print(f"[bold red]An error occurred while executing query ({error_type}):[/bold red]")
        console.print(f"  [yellow]Message:[/yellow] {e}")
        console.print("[dim]--- Traceback ---[/dim]")
        console.print(f"[dim]{traceback.format_exc()}[/dim]")

    console.print(f"\n[dim]Failed query:[/dim]")
    console.print(f"[dim]  {query}[/dim]")


def cypher_helper(query: str, context: Optional[str] = None):
    """Executes a read-only Cypher query."""
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, _, ctx = services

    from ..utils.cypher_readonly import is_read_only_cypher, read_only_rejection_message

    if not is_read_only_cypher(query):
        console.print(f"[bold red]Error:[/bold red] {read_only_rejection_message()}")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    try:
        with db_manager.get_driver().session() as session:
            result = session.run(query)
            records = [record.data() for record in result]
            console.print(json.dumps(records, indent=2))
    except Exception as e:
        _print_query_exception(e, query)
        db_manager.close_driver()
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def cypher_helper_visual(query: str, context: Optional[str] = None):
    """Executes a read-only Cypher query and visualizes the results."""
    from .visualizer import visualize_cypher_results
    from ..utils.cypher_readonly import is_read_only_cypher, read_only_rejection_message

    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, _, ctx = services

    if not is_read_only_cypher(query):
        console.print(f"[bold red]Error:[/bold red] {read_only_rejection_message()}")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    try:
        visualize_cypher_results(query)
    except Exception as e:
        _print_query_exception(e, query)
        db_manager.close_driver()
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


from ..viz.server import run_server, set_db_manager

_OFFLINE_VIZ_DEFAULT_QUERY = """
MATCH (n)-[r]->(m)
RETURN n, r, m
LIMIT 300
"""


def _render_offline_visualization(
    db_manager,
    repo_path: Optional[str] = None,
    cypher_query: Optional[str] = None,
    output_path: Optional[str] = None,
) -> None:
    """Render the graph with the dependency-free single-file HTML renderer
    (``utils/visualize_graph.py``)."""
    from ..utils.visualize_graph import open_in_browser, build_graph_data

    query = cypher_query or _OFFLINE_VIZ_DEFAULT_QUERY
    nodes: Dict[Any, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    def _ident(value) -> Optional[str]:
        return None if value is None else str(value)

    # Node / relationship classification lives in utils/graph_shapes, shared
    # with the live web renderer in viz/server.py.
    _meta = graph_shapes.meta
    _is_relationship = graph_shapes.is_relationship
    _is_node = graph_shapes.is_node

    def _node_payload(value) -> Dict[str, Any]:
        props, label = dict(value), _meta(value, "_label", "Node")
        node_id = _meta(value, "_id")
        if node_id is None:
            node_id = props.get("path") or props.get("name")
        
        name = props.get("name") or props.get("path") or ""
        if name == "<module>":
            _fpath = props.get("path") or props.get("file_path") or ""
            if _fpath:
                name = Path(_fpath).name
            else:
                name = "<module>"

        return {
            "id": _ident(node_id),
            "label": label,
            "name": name,
            "file_path": props.get("path", ""),
            "line_number": props.get("line_number"),
        }

    def _edge_payload(value) -> Dict[str, Any]:
        return {
            "source": _ident(_meta(value, "_src")),
            "target": _ident(_meta(value, "_dst")),
            "type": _meta(value, "_label", "RELATED"),
        }

    with db_manager.get_driver().session() as session:
        for record in session.run(query):
            values = list(record.values()) if hasattr(record, "values") else list(record)
            for value in values:
                if _is_node(value):
                    payload = _node_payload(value)
                    if payload["id"] is not None:
                        nodes[payload["id"]] = payload
                elif _is_relationship(value):
                    edge = _edge_payload(value)
                    if edge and edge["source"] and edge["target"]:
                        edges.append(edge)

    if not nodes:
        console.print(
            "[yellow]No graph data returned — index a repository first "
            "with 'cviz index <path>'.[/yellow]"
        )
        raise typer.Exit(code=1)

    title = f"CViz Code Graph — {Path(repo_path).name}" if repo_path else "CViz Code Graph"
    graph_data = build_graph_data(list(nodes.values()), edges)
    path = open_in_browser(graph_data, title=title, output_path=output_path)
    console.print(
        f"[green]Rendered {len(nodes)} nodes and {len(edges)} edges to[/green] {path}"
    )


def visualize_helper(
    repo_path: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_GRAPH_PORT,
    context: Optional[str] = None,
    cypher_query: Optional[str] = None,
):
    """Opens the whole graph (or a Cypher query's result) as a single HTML page.

    *host* and *port* are kept for the command's options; the page is a local
    file, no server is started.
    """
    services = _initialize_services(context, cwd=Path(repo_path).resolve() if repo_path else None)
    if not all(services[:3]):
        _fail_services_init()
    db_manager = services[0]
    try:
        return _render_offline_visualization(db_manager, repo_path=repo_path, cypher_query=cypher_query)
    finally:
        db_manager.close_driver()


def graph_start_url(base_url: str, target: Optional[str], changes: Optional[str] = None) -> str:
    """Page opened by ``cviz graph [target] [--changes BRANCH]``.

    A folder (``cviz graph <repo>``) only picks the repository served: the
    page opens on its entry points, not on the folder as an artifact.
    """
    from urllib.parse import quote

    from .open_in_graph import graph_url, parse_location

    if changes:
        return f"{base_url}/graph?changes={quote(changes, safe='')}"
    if target:
        path_str, line = parse_location(target)
        if not Path(path_str).is_dir():
            return graph_url(base_url, str(Path(path_str).resolve()), line)
    return f"{base_url}/graph"


def graph_helper(
    target: Optional[str] = None,
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    context: Optional[str] = None,
    watch_path: Optional[str] = None,
    open_browser: bool = True,
    use_polling: Optional[bool] = None,
    full_resync_hours: float = 0,
    changes: Optional[str] = None,
):
    """Serve the standalone dependency-graph page (``/graph``).

    With ``watch_path`` the incremental watcher runs in the same process and
    its updates reach open pages through ``/api/events``. Running both in one
    process also avoids the embedded backends' single-writer lock that a
    separate ``cviz watch`` would contend for.
    """
    from . import server_registry
    from .open_in_graph import parse_location

    # The database is the one of the watched repository, else of the target
    # artifact's repository, else of the current folder.
    anchor = watch_path or (parse_location(target)[0] if target else None)
    # A server already serves this repository (started by the page earlier, or
    # by the MCP server of an IDE): open it rather than fail on the database lock.
    try:
        running = server_registry.server_for(Path(anchor or Path.cwd()).resolve())
    except Exception:  # noqa: BLE001
        running = None
    if running:
        url = graph_start_url(running, target, changes)
        console.print(f"[green]The graph server of this repository already runs: {url}[/green]")
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        return
    services = _initialize_services(context, cwd=Path(anchor).resolve() if anchor else None)
    if not all(services[:3]):
        _fail_services_init()
    db_manager, graph_builder, code_finder, ctx = services
    set_db_manager(db_manager)

    repo_root = getattr(ctx, "repo_root", "")
    public_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    if port is None and repo_root:
        # One server per repository: the default port, or the next one not
        # used by another repository's server; recorded at once in the server
        # registry so two servers starting together cannot collide.
        port = server_registry.claim_port(
            repo_root, DEFAULT_GRAPH_PORT, host if host != "0.0.0.0" else "127.0.0.1", public_host)
    elif port is None:
        port = DEFAULT_GRAPH_PORT
    base_url = f"http://{public_host}:{port}"
    url = graph_start_url(base_url, target, changes)

    watcher = None
    deferred: dict = {}
    if watch_path:
        watcher = _start_graph_watcher(
            watch_path, graph_builder, code_finder, ctx, use_polling, full_resync_hours,
            deferred_out=deferred,
        )
    from ..viz.graph_api import set_index_services

    set_index_services(graph_builder, watcher, ctx.cvizignore_path, getattr(ctx, "repo_root", "") or None)
    # Vectors the index left out (cviz index without --embed) are computed
    # in the background; open pages show the progress on the live badge.
    from ..viz.ai_api import catch_up_vectorization

    if deferred:
        # Staged first index: the rest of the repository is indexed in the
        # background, then vectorized.
        from ..core.staged_index import start_deferred_index

        console.print(
            f"[yellow]{len(deferred['files'])} more files (deeper folders, tests) are indexed "
            "in the background; the page shows the progress.[/yellow]"
        )
        start_deferred_index(deferred, watcher, watch_path, on_done=catch_up_vectorization)
    else:
        catch_up_vectorization()

    console.print(f"[green]Dependency graph server on {base_url}/graph[/green]")
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            "[yellow]Listening beyond localhost: remote clients must send CVIZ_API_KEY.[/yellow]"
        )

    if open_browser:
        import threading
        import webbrowser

        def _open():
            time.sleep(1.2)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    if repo_root:
        # cviz-open, cviz update (Git hooks) and the IDE extensions find the
        # server of a repository here.
        server_registry.register(repo_root, base_url)
    try:
        run_server(host=host, port=port)
    except Exception as e:
        console.print(f"[bold red]An error occurred while running the server:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        if repo_root:
            server_registry.unregister(repo_root)
        if watcher is not None:
            watcher.stop()
        db_manager.close_driver()


def _graph_first_pass_files() -> Optional[int]:
    """GRAPH_FIRST_PASS_FILES as a file count, or None for all at once."""
    raw = str(get_config_value("GRAPH_FIRST_PASS_FILES") or "").strip().lower()
    try:
        count = int(raw)
    except ValueError:
        return None  # "unlimited", empty or invalid
    return count if count > 0 else None


def _start_graph_watcher(watch_path, graph_builder, code_finder, ctx, use_polling, full_resync_hours,
                         deferred_out: Optional[dict] = None):
    """Index if needed, then watch *watch_path* with start-up resynchronisation.

    A first index is staged (GRAPH_FIRST_PASS_FILES): *deferred_out* then
    receives the files left for start_deferred_index."""
    from ..core.watcher import CodeWatcher

    path_obj = Path(watch_path).resolve()
    if not path_obj.is_dir():
        console.print(f"[red]Error: --watch needs a directory: {path_obj}[/red]")
        raise typer.Exit(code=1)
    indexed = any_repo_matches_path(code_finder.list_indexed_repositories(), path_obj)
    if not indexed:
        console.print(f"[yellow]Indexing {path_obj} first...[/yellow]")
        # The server vectorizes once it is up (catch_up_vectorization).
        graph_builder.semantic_search = False
        try:
            asyncio.run(graph_builder.build_graph_from_path_async(
                path_obj, is_dependency=False, cvizignore_path=ctx.cvizignore_path,
                first_pass_files=_graph_first_pass_files() if deferred_out is not None else None,
                deferred_out=deferred_out))
        finally:
            graph_builder.semantic_search = None
    watcher = CodeWatcher(graph_builder, JobManager(), use_polling=use_polling)
    watcher.start()
    watcher.watch_directory(
        str(path_obj),
        perform_initial_scan=False,
        # Catch up with edits made while nothing was watching (only the
        # files changed since the last recorded update).
        catch_up_on_start=indexed,
        cvizignore_path=ctx.cvizignore_path,
        full_resync_hours=full_resync_hours,
    )
    console.print(f"[green]Watching {path_obj} — saves are pushed to open graph pages.[/green]")
    return watcher


def reindex_helper(path: str, context: Optional[str] = None, no_progress: bool = False,
                   embed: Optional[bool] = None):
    """Force re-index by deleting and rebuilding the repository."""
    time_start = time.time()
    path_obj = Path(path).resolve()
    index_cwd = path_obj if path_obj.is_dir() else path_obj.parent
    services = _initialize_services(context, cwd=index_cwd)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, code_finder, ctx = services
    graph_builder.semantic_search = embed

    if not path_obj.exists():
        console.print(f"[red]Error: Path does not exist: {path_obj}[/red]")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    # Check if already indexed
    indexed_repos = code_finder.list_indexed_repositories()
    repo_exists = any_repo_matches_path(indexed_repos, path_obj)

    if repo_exists:
        console.print(f"[yellow]Deleting existing index for: {path_obj}[/yellow]")
        try:
            graph_builder.delete_repository_from_graph(str(path_obj))
            # Companion sources are indexed with the repository: rebuilt with it.
            from ..core import companions

            indexed_paths = {Path(r.get("path", "")).as_posix() for r in indexed_repos if r.get("path")}
            for companion in sorted(set(companions.indexed_roots(path_obj))
                                    | {p.as_posix() for p in companions.optional_roots(path_obj)}):
                if companion in indexed_paths:
                    graph_builder.delete_repository_from_graph(companion)
            console.print("[green]✓[/green] Deleted old index")
        except Exception as e:
            console.print(f"[red]Error deleting old index: {e}[/red]")
            db_manager.close_driver()
            raise typer.Exit(code=1)
    
    console.print(f"[cyan]Re-indexing: {path_obj}[/cyan]")
    _print_companions(path_obj)
    
    try:
        asyncio.run(_run_index_with_progress(graph_builder, path_obj, is_dependency=False, cvizignore_path=ctx.cvizignore_path, disable_progress=no_progress))
        time_end = time.time()
        elapsed = time_end - time_start
        _print_call_resolution_diagnostics(graph_builder)
        _print_index_execution_summary(graph_builder)
        console.print(f"[green]Successfully re-indexed: {path} in {elapsed:.2f} seconds[/green]")
        _print_deferred_vectorization_tip(embed)
    except Exception as e:
        console.print(f"[bold red]An error occurred during re-indexing:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def update_helper(path: str, context: Optional[str] = None, quiet: bool = False, full: bool = False):
    """Bring the index of the repository containing *path* in line with the disk.

    Only the files whose content differs from the graph are re-processed
    (content fingerprints on the File nodes). When a ``cviz graph`` server
    holds the database (LadybugDB accepts a single process), it is asked to
    do the update itself. ``full`` deletes and rebuilds the repository, the
    former behaviour. *quiet* (Git hooks) suppresses the console output.
    """
    previous_quiet = console.quiet
    console.quiet = quiet or previous_quiet
    try:
        if full:
            console.print("[cyan]Rebuilding the repository index...[/cyan]")
            reindex_helper(path, context)
            _publish_index_done(path)
            return
        result = _update_through_server(path)
        if result is None:
            result = _update_locally(path, context)
        if result is not None:
            _print_update_result(result)
    finally:
        console.quiet = previous_quiet


def _print_update_result(result: dict) -> None:
    mode = result.get("mode")
    where = " (by the running graph server)" if result.get("server") else ""
    if mode == "none":
        console.print(f"[green]Index already up to date{where}.[/green]")
    else:
        console.print(
            f"[green]Index updated{where}:[/green] {result.get('changed', 0)} changed, "
            f"{result.get('removed', 0)} removed file(s) ({mode})."
        )


def _update_through_server(path: str) -> Optional[dict]:
    """Ask a running ``cviz graph`` server to update; None when there is none."""
    from . import server_registry
    from .open_in_graph import _request

    # The server of this repository (one per repository), or an explicit URL.
    server = os.getenv("CVIZ_GRAPH_URL") or server_registry.server_for(path)
    if not server:
        return None
    server = server.rstrip("/")
    try:
        result = _request(f"{server}/api/update", {"path": str(Path(path).resolve())}, timeout=1800)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 503):  # not this server's repository / no index services
            return None
        console.print(f"[yellow]The graph server could not update ({exc.code}); updating here.[/yellow]")
        return None
    except Exception:  # noqa: BLE001 - no server: update in this process
        return None
    result["server"] = server
    return result


def _update_locally(path: str, context: Optional[str]) -> Optional[dict]:
    from ..core.watcher import incremental_update

    path_obj = Path(path).resolve()
    services = _initialize_services(context, cwd=path_obj if path_obj.is_dir() else path_obj.parent)
    if not all(services[:3]):
        _fail_services_init()
    db_manager, graph_builder, code_finder, ctx = services
    try:
        roots = [
            Path(str(r["path"])) for r in code_finder.list_indexed_repositories()
            if r.get("path") and not r.get("is_dependency")
        ]
        target = path_obj.as_posix()
        matching = [r for r in roots if target == r.as_posix() or target.startswith(r.as_posix().rstrip("/") + "/")]
        if not matching:
            db_manager.close_driver()
            console.print(f"[yellow]{path_obj} is not indexed yet: indexing it.[/yellow]")
            index_helper(str(path_obj), context)
            _publish_index_done(str(path_obj))
            return None
        repo = max(matching, key=lambda r: len(r.as_posix()))
        return incremental_update(graph_builder, repo, cvizignore_path=ctx.cvizignore_path)
    finally:
        db_manager.close_driver()


def _publish_index_done(path: str) -> None:
    """Tell open graph pages that *path* was re-indexed (Git hooks, CLI)."""
    try:
        from ..core import graph_events

        graph_events.publish(graph_events.INDEX_DONE, repo=Path(path).resolve().as_posix())
    except Exception:  # noqa: BLE001 - notification is best effort
        pass


def clean_helper(context: Optional[str] = None):
    """Remove orphaned nodes and relationships from the database."""
    if not is_db_deletion_allowed():
        console.print(
            "[bold red]Error:[/bold red] Database cleanup is disabled. "
            "Set ALLOW_DB_DELETION=true in config to enable."
        )
        raise typer.Exit(code=1)

    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, _, ctx = services
    
    console.print("[cyan]🧹 Cleaning database (removing orphaned nodes)...[/cyan]")
    
    try:
        total_deleted = 0
        batch_size = 500
        
        with db_manager.get_driver().session() as session:
            # Delete nodes with no incoming relationships (true orphans).
            # Parameters (HAS_PARAMETER), import Modules (IMPORTS), etc. are kept.
            while True:
                result = session.run("""
                    MATCH (n)
                    WHERE NOT n:Repository
                      AND NOT ()-[]->(n)
                    WITH n LIMIT $batch_size
                    DETACH DELETE n
                    RETURN count(n) as deleted
                """, batch_size=batch_size)
                record = result.single()
                deleted_count = record["deleted"] if record else 0
                total_deleted += deleted_count
                
                if deleted_count == 0:
                    break
                    
                console.print(f"[dim]  Deleted {deleted_count} orphaned nodes (batch)...[/dim]")
            
            if total_deleted > 0:
                console.print(f"[green]✓[/green] Deleted {total_deleted} orphaned nodes total")
            else:
                console.print("[green]✓[/green] No orphaned nodes found")
            
        console.print("[green]✅ Database cleanup complete![/green]")
    except Exception as e:
        console.print(f"[bold red]An error occurred during cleanup:[/bold red] {e}")
    finally:
        db_manager.close_driver()


def stats_helper(path: str = None, context: Optional[str] = None):
    """Show indexing statistics for a repository or overall."""
    services = _initialize_services(context, cwd=Path(path).resolve() if path else None)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, code_finder, ctx = services
    
    try:
        if path:
            # Stats for specific repository
            path_obj = Path(path).resolve()
            # Paths are stored with forward slashes (as_posix) in the graph DB,
            # so lookups must use the same normalization on Windows too.
            repo_path_str = path_obj.as_posix()
            console.print(f"[cyan]📊 Statistics for: {path_obj}[/cyan]\n")
            
            with db_manager.get_driver().session() as session:
                # Get repository node
                repo_query = """
                MATCH (r:Repository {path: $path})
                RETURN r
                """
                result = session.run(repo_query, path=repo_path_str)
                if not result.single():
                    console.print(f"[red]Repository not found: {path_obj}[/red]")
                    return
                
                # Get stats
                # Get stats using separate queries to handle depth and avoid Cartesian products.
                # `count(x)` counts *rows*, and a variable-length CONTAINS* match
                # yields one row per distinct path to the node: a method is
                # reachable as Repo->..->File->Function and again as
                # Repo->..->File->Class->Function, a nested function three ways.
                # count(DISTINCT x) is required or the totals are inflated —
                # functions were over-reported by ~55% on CViz's own repo.
                # 1. Files
                file_query = "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(f:File) RETURN count(DISTINCT f) as c"
                file_count = session.run(file_query, path=repo_path_str).single()["c"]

                # 2. Functions (including methods in classes)
                func_query = "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(func:Function) RETURN count(DISTINCT func) as c"
                func_count = session.run(func_query, path=repo_path_str).single()["c"]

                # 3. Classes
                class_query = "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(c:Class) RETURN count(DISTINCT c) as c"
                class_count = session.run(class_query, path=repo_path_str).single()["c"]
                
                # 4. Modules (imported) - Note: Module nodes are outside the repo structure usually, connected via IMPORTS
                # We need to traverse from files to modules
                module_query = "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(f:File)-[:IMPORTS]->(m:Module) RETURN count(DISTINCT m) as c"
                module_count = session.run(module_query, path=repo_path_str).single()["c"]

                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("Metric", style="cyan")
                table.add_column("Count", style="green", justify="right")
                
                table.add_row("Files", str(file_count))
                table.add_row("Functions", str(func_count))
                table.add_row("Classes", str(class_count))
                table.add_row("Imported Modules", str(module_count))
                
                console.print(table)
        else:
            # Overall stats
            console.print("[cyan]📊 Overall Database Statistics[/cyan]\n")
            
            with db_manager.get_driver().session() as session:
                # Get overall counts using separate O(1) queries
                repo_count = session.run("MATCH (r:Repository) RETURN count(r) as c").single()["c"]
                
                if repo_count > 0:
                    file_count = session.run("MATCH (f:File) RETURN count(f) as c").single()["c"]
                    func_count = session.run("MATCH (f:Function) RETURN count(f) as c").single()["c"]
                    class_count = session.run("MATCH (c:Class) RETURN count(c) as c").single()["c"]
                    module_count = session.run("MATCH (m:Module) RETURN count(m) as c").single()["c"]
                    
                    # Extended node types (PHP, Rust, Go, etc.)
                    interface_count = session.run("MATCH (i:Interface) RETURN count(i) as c").single()["c"]
                    trait_count = session.run("MATCH (t:Trait) RETURN count(t) as c").single()["c"]
                    struct_count = session.run("MATCH (s:Struct) RETURN count(s) as c").single()["c"]
                    enum_count = session.run("MATCH (e:Enum) RETURN count(e) as c").single()["c"]
                    
                    table = Table(show_header=True, header_style="bold magenta")
                    table.add_column("Metric", style="cyan")
                    table.add_column("Count", style="green", justify="right")
                    
                    table.add_row("Repositories", str(repo_count))
                    table.add_row("Files", str(file_count))
                    table.add_row("Functions", str(func_count))
                    table.add_row("Classes", str(class_count))
                    if interface_count > 0:
                        table.add_row("Interfaces", str(interface_count))
                    if trait_count > 0:
                        table.add_row("Traits", str(trait_count))
                    if struct_count > 0:
                        table.add_row("Structs", str(struct_count))
                    if enum_count > 0:
                        table.add_row("Enums", str(enum_count))
                    table.add_row("Modules", str(module_count))
                    
                    console.print(table)
                else:
                    console.print("[yellow]No data indexed yet.[/yellow]")
                    
    except Exception as e:
        console.print(f"[bold red]An error occurred:[/bold red] {e}")
    finally:
        db_manager.close_driver()


def watch_helper(
    path: str,
    context: Optional[str] = None,
    use_polling: Optional[bool] = None,
    sync_on_start: bool = False,
    catch_up: bool = False,
    full_resync_hours: float = 0,
):
    """Watch a directory for changes and auto-update the graph (blocking mode)."""
    import logging
    from ..core.watcher import CodeWatcher
    
    # Suppress verbose watchdog DEBUG logs
    logging.getLogger('watchdog').setLevel(logging.WARNING)
    logging.getLogger('watchdog.observers').setLevel(logging.WARNING)
    logging.getLogger('watchdog.observers.inotify_buffer').setLevel(logging.WARNING)
    
    services = _initialize_services(context, cwd=Path(path).resolve())
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, code_finder, ctx = services
    path_obj = Path(path).resolve()

    if not path_obj.exists():
        console.print(f"[red]Error: Path does not exist: {path_obj}[/red]")
        db_manager.close_driver()
        raise typer.Exit(code=1)
    
    if not path_obj.is_dir():
        console.print(f"[red]Error: Path must be a directory: {path_obj}[/red]")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    console.print(f"[bold cyan]🔍 Watching {path_obj} for changes...[/bold cyan]")
    
    # Check if already indexed — use File node count as a robust fallback so a
    # transient empty result from list_indexed_repositories never triggers a
    # destructive full rescan of an already-populated graph.
    indexed_repos = code_finder.list_indexed_repositories()
    is_indexed = any_repo_matches_path(indexed_repos, path_obj)
    if not is_indexed:
        # Fallback: count File nodes whose path starts with this repo's path.
        # If > 100 exist, the repo is clearly already indexed — skip the scan.
        try:
            with code_finder.driver.session() as _s:
                _r = _s.run(
                    "MATCH (n:File) WHERE n.path STARTS WITH $p RETURN count(n) AS c",
                    p=path_obj.as_posix() + "/"
                )
                _count = _r.single()["c"]
            if _count > 100:
                info_logger(
                    f"[watch] list_indexed_repositories returned no match for {path_obj} "
                    f"but {_count} File nodes exist — treating as already indexed."
                )
                is_indexed = True
        except Exception as _e:
            warning_logger(f"[watch] Fallback indexed check failed: {_e}")
    
    # Create watcher instance
    job_manager = JobManager()
    watcher = CodeWatcher(graph_builder, job_manager, use_polling=use_polling)
    
    try:
        # Start the observer thread
        watcher.start()
        
        # Add the directory to watch
        if is_indexed:
            if sync_on_start:
                console.print("[green]✓[/green] Already indexed. Synchronizing current files...")
            elif catch_up:
                console.print("[green]✓[/green] Already indexed. Catching up with changes made since the last update...")
            else:
                console.print("[green]✓[/green] Already indexed. Watching for future changes only.")
                console.print(
                    "[dim]Use 'cviz index --force' or 'cviz watch --sync-on-start' to reconcile existing changes.[/dim]"
                )
            extra = {}
            if catch_up and not sync_on_start:
                extra["catch_up_on_start"] = True
            if full_resync_hours:
                extra["full_resync_hours"] = full_resync_hours
            watcher.watch_directory(
                str(path_obj),
                perform_initial_scan=False,
                sync_on_start=sync_on_start,
                cvizignore_path=ctx.cvizignore_path,
                **extra,
            )
        else:
            console.print("[yellow]⚠[/yellow]  Not indexed yet. Performing initial scan...")
            
            # Index the repository first (like MCP does)
            async def do_index():
                await graph_builder.build_graph_from_path_async(
                    path_obj,
                    is_dependency=False,
                    cvizignore_path=ctx.cvizignore_path,
                )
            
            asyncio.run(do_index())
            console.print("[green]✓[/green] Initial scan complete")
            
            # Now start watching (without another scan)
            watcher.watch_directory(
                str(path_obj),
                perform_initial_scan=False,
                cvizignore_path=ctx.cvizignore_path,
            )
        
        console.print("[bold green]👀 Monitoring for file changes...[/bold green] (Press Ctrl+C to stop)")
        console.print("[dim]💡 Tip: Open a new terminal window to continue working[/dim]\n")
        
        # Block here and keep the watcher running
        import threading
        stop_event = threading.Event()
        
        try:
            stop_event.wait()  # Wait indefinitely until interrupted
        except KeyboardInterrupt:
            console.print("\n[yellow]🛑 Stopping watcher...[/yellow]")
            
    except KeyboardInterrupt:
        console.print("\n[yellow]🛑 Stopping watcher...[/yellow]")
    except Exception as e:
        console.print(f"[bold red]An error occurred:[/bold red] {e}")
    finally:
        watcher.stop()
        db_manager.close_driver()
        console.print("[green]✓[/green] Watcher stopped.")



def unwatch_helper(path: str):
    """Report that unwatching is not available from the CLI.

    This command touches no watcher state at all — it never has. It used to
    print a note, echo the requested path back as "Path specified:" (which
    reads like confirmation that something happened, even for a path that was
    never watched and does not exist) and exit 0.
    """
    console.print(
        "[bold red]Error:[/bold red] 'cviz unwatch' is not supported from the CLI — "
        "it cannot reach a watcher started in another process."
    )
    console.print("[dim]For CLI watch mode, press Ctrl+C in the terminal running 'cviz watch'.[/dim]")
    console.print(
        "[dim]For MCP mode, call the 'unwatch_directory' tool from your IDE.[/dim]"
    )
    raise typer.Exit(code=1)


def list_watching_helper():
    """Report that listing watched paths is not available from the CLI.

    Same as unwatch: no watcher state is consulted, so exiting 0 advertised a
    successful listing of nothing.
    """
    console.print(
        "[bold red]Error:[/bold red] 'cviz watching' is not supported from the CLI — "
        "it cannot reach a watcher started in another process."
    )
    console.print("[dim]For CLI watch mode, check the terminal where you ran 'cviz watch'.[/dim]")
    console.print("[dim]For MCP mode: start 'cviz mcp start', then call the "
                  "'list_watched_paths' tool from your IDE.[/dim]")
    raise typer.Exit(code=1)


def setup_scip_helper() -> None:
    """Diagnostic and setup helper for SCIP indexers."""
    from ..tools.scip_indexer import EXTENSION_TO_SCIP
    import shutil
    
    console.print("[bold cyan]🔍 Checking SCIP Indexer Availability...[/bold cyan]\n")
    
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Language", style="cyan")
    table.add_column("Binary", style="yellow")
    table.add_column("Status", style="green")
    table.add_column("Install Hint", style="dim")
    
    langs = {}
    for ext, (lang, binary, hint, docker) in EXTENSION_TO_SCIP.items():
        if lang not in langs:
            langs[lang] = (binary, hint, docker)
            
    for lang, (binary, hint, docker) in sorted(langs.items()):
        is_installed = shutil.which(binary) is not None
        status = "[green]✓ Installed[/green]" if is_installed else "[red]✗ Not Found[/red]"
        table.add_row(lang, binary, status, hint)
        
    console.print(table)
    
    # Check Docker
    has_docker = shutil.which("docker") is not None
    if has_docker:
        console.print("\n[green]✓ Docker is available (Auto-fallback enabled)[/green]")
    else:
        console.print("\n[yellow]⚠ Docker not found. Local binaries are required for SCIP.[/yellow]")

    console.print("\n[dim]To enable SCIP indexing, run:[/dim]")
    console.print("[bold white]cviz config set SCIP_INDEXER true[/bold white]")


def embed_helper(path: Optional[str], all_repos: bool = False, context: Optional[str] = None) -> None:
    """Vectorize indexed repositories for the natural-language search (cviz embed)."""
    from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

    from ..semantic import settings as ai_settings
    from ..semantic.embedders import probe
    from ..semantic.indexer import sync

    from rich.markup import escape

    embed_cfg = ai_settings.embed_settings()

    services = _initialize_services(context, cwd=Path(path).resolve() if path else None)
    if not all(services[:3]):
        _fail_services_init()
    db_manager, _, code_finder, _ctx = services
    try:
        indexed = [
            str(r.get("path")).replace("\\", "/").rstrip("/")
            for r in code_finder.list_indexed_repositories()
            if r.get("path") and not r.get("is_dependency")
        ]
        if all_repos:
            targets = indexed
        else:
            wanted = Path(path or ".").resolve().as_posix()
            targets = [r for r in indexed if wanted == r or wanted.startswith(r + "/")]
            if not targets:
                console.print(f"[bold red]{wanted} is not inside an indexed repository.[/bold red] "
                              "Run 'cviz index' first, or use --all.")
                raise typer.Exit(code=1)
            targets = [max(targets, key=len)]
        if not targets:
            console.print("[yellow]No repositories indexed yet.[/yellow]")
            return
        # Checked once there is something to vectorize: an empty index needs no model.
        problem = probe(embed_cfg)
        if problem:
            console.print(f"[bold red]Cannot vectorize:[/bold red] {escape(problem)}")
            raise typer.Exit(code=1)

        console.print(f"[cyan]Embedding model:[/cyan] {embed_cfg.model_id}")
        driver = db_manager.get_driver()
        failed = False
        with Progress(TextColumn("{task.description}"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
                      TimeElapsedColumn(), console=console) as progress:
            for repo in targets:
                task = progress.add_task(Path(repo).name, total=None)

                def _advance(done: int, total: int, _task=task) -> None:
                    progress.update(_task, completed=done, total=total)

                result = sync(driver.session, repo, embed_settings=embed_cfg, progress=_advance)
                progress.update(task, total=max(result["embedded"], 1), completed=max(result["embedded"], 1))
                if result.get("error"):
                    failed = True
                    progress.console.print(f"[red]{repo}: {result['error']}[/red]")
                else:
                    progress.console.print(
                        f"[green]{repo}[/green]: {result['items']} artifacts, {result['embedded']} new vectors"
                    )
        if not ai_settings.enabled():
            console.print("[dim]Tip: 'cviz config set SEMANTIC_SEARCH true' keeps the vectors up to date "
                          "on every index / watch.[/dim]")
        if failed:
            raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()
