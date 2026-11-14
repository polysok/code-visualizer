# src/code_visualizer/tools/indexing/pipeline.py
"""Orchestrates full-repo indexing (Tree-sitter path)."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import os
from ...core.jobs import JobManager, JobStatus
from ...utils.debug_log import debug_log, error_logger, info_logger, warning_logger
from .discovery import discover_files_to_index, prioritize_files
from .dotnet_packages import external_type_origins
from .persistence.writer import GraphWriter
from .pre_scan import pre_scan_for_imports
from .resolution.calls import build_function_call_groups
from .resolution.inheritance import (
    build_binds_links,
    build_companion_of_links,
    build_decorated_by_links,
    build_elixir_implements_links,
    build_embeds_links,
    build_go_implements_links,
    build_haskell_implements_links,
    build_inheritance_and_csharp_files,
    build_metaclass_links,
    build_partial_of_links,
    build_part_of_links,
    build_previews_links,
)


DEFAULT_PARALLEL_WORKERS = 10
# Used only when PARALLEL_WORKERS is empty or not a positive integer.
# load_config() copies DEFAULT_CONFIG first, where the key is "4", so a
# normal install reads 4 for both indexing and the watcher. This smaller
# fallback is for the watcher alone: incremental re-parse runs in the
# background during editing.
WATCHER_DEFAULT_PARALLEL_WORKERS = 4
MAX_PARALLEL_WORKERS = 32


def _clamp_parallel_workers(workers: int) -> int:
    """Cap a resolved worker count. A config of 5000 must not spawn 5000 threads."""
    if workers > MAX_PARALLEL_WORKERS:
        warning_logger(
            f"PARALLEL_WORKERS={workers} exceeds {MAX_PARALLEL_WORKERS}; "
            f"using {MAX_PARALLEL_WORKERS}"
        )
        return MAX_PARALLEL_WORKERS
    return workers


def get_parallel_workers(default: int = DEFAULT_PARALLEL_WORKERS) -> int:
    """Resolve PARALLEL_WORKERS for indexing and the file watcher.

    ``default`` is used when the value is empty or not a positive integer.
    A full index uses ``DEFAULT_PARALLEL_WORKERS`` (10). The watcher passes
    ``WATCHER_DEFAULT_PARALLEL_WORKERS`` (4). A positive integer is returned
    to every caller, capped at ``MAX_PARALLEL_WORKERS``. ``load_config``
    supplies ``"4"`` from ``DEFAULT_CONFIG`` when the user has not set the
    key, so both paths use 4 in that case.

    A non-integer or non-positive value logs a warning before falling back.
    """
    from ...cli.config_manager import get_config_value

    raw = get_config_value("PARALLEL_WORKERS")
    if raw is None or str(raw).strip() == "":
        return _clamp_parallel_workers(default)

    try:
        workers = int(str(raw).strip())
    except (TypeError, ValueError):
        warning_logger(f"Invalid PARALLEL_WORKERS={raw!r}; using {default}")
        return _clamp_parallel_workers(default)

    if workers <= 0:
        warning_logger(f"Invalid PARALLEL_WORKERS={raw!r}; using {default}")
        return _clamp_parallel_workers(default)

    return _clamp_parallel_workers(workers)


def build_index_summary(
    files: List[Path],
    parsers: Dict[str, str],
    all_file_data: List[Dict[str, Any]],
    resolved_call_groups: tuple,
    serialization_seconds: float,
    parse_failures: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build CLI-facing metrics for a completed Tree-sitter indexing run."""
    extension_counts = Counter()
    for file in files:
        extension = file.suffix or file.name
        language = parsers.get(file.suffix, "generic")
        extension_counts[f"{extension} ({language})"] += 1

    # Exclude the synthetic `<module>` frame the Python parser adds per file —
    # it is the attribution target for module-level calls, not a function the
    # user wrote, and counting it inflated every reported total by one per file.
    function_nodes = sum(
        1
        for file_data in all_file_data
        for func in file_data.get("functions", [])
        if not func.get("is_synthetic") and func.get("name") != "<module>"
    )
    class_nodes = sum(len(file_data.get("classes", [])) for file_data in all_file_data)
    call_edges = sum(len(group) for group in resolved_call_groups)

    failed = list(parse_failures or [])

    return {
        "total_scanned_files": len(files),
        "files_by_extension": dict(sorted(extension_counts.items())),
        "function_nodes": function_nodes,
        "class_nodes": class_nodes,
        "call_edges": call_edges,
        "serialization_seconds": serialization_seconds,
        # A run where files failed to parse used to be indistinguishable from a
        # clean one: the summary had no failure row at all.
        "failed_files": len(failed),
        "failed_file_details": failed,
    }


@dataclass
class _IndexContext:
    """What the stages of one index share (see run_tree_sitter_index_async)."""
    path: Path
    is_dependency: bool
    job_id: Optional[str]
    job_manager: JobManager
    writer: GraphWriter
    repo_name: str
    companion_of: Dict[Path, Tuple[Path, bool]]
    imports_map: dict
    parse_file: Callable[[Path, Path, bool], Dict[str, Any]]
    add_minimal_file_node: Callable[[Path, Path, bool], None]
    call_resolution_diagnostics: Optional[List[Dict[str, Any]]]
    semantic_search: Optional[bool]
    companion_roots: Sequence[Path]


async def _parse_and_write(
    ctx: _IndexContext,
    files: Sequence[Path],
    progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """Parse ``files`` and write their nodes: (file data, parse failures, write start)."""
    path = ctx.path
    is_dependency = ctx.is_dependency
    job_id = ctx.job_id
    job_manager = ctx.job_manager
    writer = ctx.writer
    repo_name = ctx.repo_name
    companion_of = ctx.companion_of
    imports_map = ctx.imports_map
    parse_file = ctx.parse_file
    add_minimal_file_node = ctx.add_minimal_file_node
    call_resolution_diagnostics = ctx.call_resolution_diagnostics
    semantic_search = ctx.semantic_search
    companion_roots = ctx.companion_roots
    all_file_data: List[Dict[str, Any]] = []
    resolved_repo_path_str = path.resolve().as_posix() if path.is_dir() else path.parent.resolve().as_posix()

    processed_count = 0
    concurrency_limit = get_parallel_workers()
    semaphore = asyncio.Semaphore(concurrency_limit)
    
    async def process_file(file: Path) -> Optional[Dict[str, Any]]:
        nonlocal processed_count
        async with semaphore:
            if not file.is_file():
                return None
            
            if job_id:
                job_manager.update_job(job_id, current_file=str(file))
            
            repo_path = path.resolve() if path.is_dir() else file.parent.resolve()
            file_is_dependency = is_dependency
            if file in companion_of:
                repo_path, file_is_dependency = companion_of[file]
            
            try:
                # 1. Parse file (CPU bound, run in thread)
                file_data = await asyncio.to_thread(parse_file, repo_path, file, file_is_dependency)
                
                file_data["_index_repo_path"] = str(repo_path)
                if file in companion_of:
                    file_data["_index_companion"] = True
                return file_data
            except Exception as e:
                error_logger(f"Error indexing file {file}: {e}")
            
            return None

    # Process all files in parallel with the semaphore limit
    tasks = [process_file(f) for f in files]
    for coro in asyncio.as_completed(tasks):
        file_data = await coro
        if file_data:
            all_file_data.append(file_data)
        
        processed_count += 1
        if job_id:
            job_manager.update_job(job_id, processed_files=processed_count)
        if progress is not None:
            progress(processed_count, len(files))
        
        if processed_count % 50 == 0:
            
            if os.environ.get("CVIZ_ACTIVE_PROGRESS_BAR") != "1":
                info_logger(f"Processed {processed_count}/{len(files)} files...")

    serialization_start = time.time()

    # Parsing remains concurrent, but graph writes are ordered so shared nodes
    # such as imported modules receive deterministic canonical metadata.
    # One unwritable file must not abort the run. There is no transaction here,
    # so an exception escaping this loop left a partially written graph with no
    # rollback and every remaining file silently unindexed.
    write_failures: List[Dict[str, Any]] = []
    for file_data in sorted(all_file_data, key=lambda data: str(data.get("path") or "")):
        repo_path = Path(file_data.pop("_index_repo_path"))
        if file_data.pop("_index_companion", False):
            file_repo_name, file_repo_path_str = repo_path.name, repo_path.resolve().as_posix()
            file_is_dependency = True
        else:
            file_repo_name, file_repo_path_str = repo_name, resolved_repo_path_str
            file_is_dependency = is_dependency
        try:
            if "error" not in file_data:
                await asyncio.to_thread(
                    writer.add_file_to_graph,
                    file_data,
                    file_repo_name,
                    imports_map,
                    repo_path_str=file_repo_path_str,
                )
            elif not file_data.get("unsupported"):
                await asyncio.to_thread(
                    add_minimal_file_node,
                    Path(file_data["path"]),
                    repo_path,
                    file_is_dependency,
                )
        except Exception as exc:  # noqa: BLE001 - keep indexing the other files
            # Must not be named `path`: that is the function's repo-root Path
            # parameter, still needed further down (`path.is_dir()`). Rebinding
            # it to this file's path string turned a single recoverable write
            # failure into an AttributeError that aborted the whole job.
            failed_path = file_data.get("path")
            # Retry once before giving up: writes are MERGE-idempotent, and a
            # transient failure under runner load silently costs the graph the
            # whole file's edges (observed as LadybugDB intermittently writing
            # ~51 fewer CONTAINS edges in the parity run, #1612).
            retried_ok = False
            if "error" not in file_data:
                try:
                    await asyncio.sleep(0.2)
                    await asyncio.to_thread(
                        writer.add_file_to_graph,
                        file_data,
                        repo_name,
                        imports_map,
                        repo_path_str=resolved_repo_path_str,
                    )
                    retried_ok = True
                    warning_logger(
                        f"Write for {failed_path} succeeded on retry after: {exc}"
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    exc = retry_exc
            if not retried_ok:
                write_failures.append({"path": failed_path, "error": str(exc)})
                error_logger(f"Failed to write {failed_path} to the graph: {exc}")
                file_data["error"] = str(exc)
                file_data["parse_failed"] = True

    if write_failures:
        warning_logger(
            f"{len(write_failures)} file(s) could not be written to the graph; "
            "the rest of the repository was still indexed."
        )

    # Capture genuine parse failures before the error entries are dropped —
    # build_index_summary runs much later, against the filtered list.
    parse_failures = [
        {"path": file_data.get("path"), "error": file_data.get("error")}
        for file_data in all_file_data
        if file_data.get("parse_failed")
    ]
    if parse_failures:
        warning_logger(
            f"{len(parse_failures)} file(s) failed to parse and contributed no "
            "symbols to the graph."
        )
        for failure in parse_failures[:10]:
            warning_logger(f"  parse failed: {failure['path']}: {failure['error']}")

    # Sort before post-processing. Files are appended in `asyncio.as_completed`
    # order, so the list order varies run to run with task scheduling. Everything
    # downstream that builds a map by iterating it — global_class_bases,
    # interface_implementors, class_method_index — then inherits that ordering,
    # and any last-writer-wins or first-match-wins choice becomes
    # nondeterministic. The graph write loop already sorted for exactly this
    # reason; resolution was left unsorted.
    all_file_data = sorted(
        (file_data for file_data in all_file_data if "error" not in file_data),
        key=lambda data: str(data.get("path") or ""),
    )

    return all_file_data, parse_failures, serialization_start


async def _link_and_enrich(ctx: _IndexContext, all_file_data: List[Dict[str, Any]]):
    """Inheritance, CALLS and the other cross-file links of ``all_file_data``.

    Every write here is a MERGE, so running it again over a superset of
    files (the deferred stage) only adds the links that were missing.
    """
    path = ctx.path
    is_dependency = ctx.is_dependency
    job_id = ctx.job_id
    job_manager = ctx.job_manager
    writer = ctx.writer
    repo_name = ctx.repo_name
    companion_of = ctx.companion_of
    imports_map = ctx.imports_map
    parse_file = ctx.parse_file
    add_minimal_file_node = ctx.add_minimal_file_node
    call_resolution_diagnostics = ctx.call_resolution_diagnostics
    semantic_search = ctx.semantic_search
    companion_roots = ctx.companion_roots
    resolved_repo_path_str = path.resolve().as_posix() if path.is_dir() else path.parent.resolve().as_posix()
    info_logger(
        f"File processing complete. {len(all_file_data)} files parsed. "
        f"Starting post-processing phase (inheritance + function calls)..."
    )

    t0 = time.time()
    if job_id:
        job_manager.update_job(job_id, status_message="Resolving inheritance links...")
    info_logger(f"[INHERITS] Resolving inheritance links across {len(all_file_data)} files...")
    inheritance_batch, csharp_files = await asyncio.to_thread(
        build_inheritance_and_csharp_files, all_file_data, imports_map
    )
    implements_batch = await asyncio.to_thread(build_go_implements_links, all_file_data)
    implements_batch.extend(await asyncio.to_thread(build_haskell_implements_links, all_file_data))
    implements_batch.extend(await asyncio.to_thread(build_elixir_implements_links, all_file_data))
    await asyncio.to_thread(writer.write_inheritance_links, inheritance_batch, csharp_files, imports_map)
    await asyncio.to_thread(writer.write_implements_links, implements_batch)
    # Keep graph writes sequential. Each await yields to health/status requests
    # and prevents scheduling the next phase after coroutine cancellation.
    for build_links, write_links in (
        (build_embeds_links, writer.write_embeds_links),
        (build_companion_of_links, writer.write_companion_of_links),
        (build_partial_of_links, writer.write_partial_of_links),
        (build_part_of_links, writer.write_part_of_links),
    ):
        links = await asyncio.to_thread(build_links, all_file_data)
        await asyncio.to_thread(write_links, links)
    for build_links, write_links in (
        (build_metaclass_links, writer.write_metaclass_links),
        (build_decorated_by_links, writer.write_decorated_by_links),
        (build_binds_links, writer.write_binds_links),
        (build_previews_links, writer.write_previews_links),
    ):
        links = await asyncio.to_thread(build_links, all_file_data, imports_map)
        await asyncio.to_thread(write_links, links)
    t1 = time.time()
    info_logger(f"Inheritance links created in {t1 - t0:.1f}s. Starting function calls...")

    resolved_calls = await asyncio.to_thread(
        build_function_call_groups,
        all_file_data,
        imports_map,
        None,
        diagnostics=call_resolution_diagnostics,
    )
    if job_id:
        job_manager.update_job(job_id, status_message="Writing function CALLS edges...")
    await asyncio.to_thread(writer.write_function_call_groups, *resolved_calls)
    try:
        origins = await asyncio.to_thread(external_type_origins, all_file_data, resolved_calls)
        await asyncio.to_thread(writer.write_external_class_origins, origins)
    except Exception as exc:  # noqa: BLE001 - attribution is a bonus, never fatal
        warning_logger(f"[CALLS] Package origin of external types skipped: {exc}")
    t2 = time.time()
    info_logger(f"Function calls created in {t2 - t1:.1f}s. Total post-processing: {t2 - t0:.1f}s")

    # ── C++: Class->Function edges (post-pass, after all files written) ───────
    # C++ method definitions live in .cpp while the Class node lives in .h.
    # The per-file write cannot create these edges reliably due to ordering;
    # this single repo-scoped pass runs after every node is in the graph.
    if job_id:
        job_manager.update_job(job_id, status_message="Linking C++ class-function edges...")
    info_logger("[CPP] Linking C++ out-of-line method definitions to their classes...")
    await asyncio.to_thread(writer.write_cpp_class_function_links, resolved_repo_path_str)

    # ── Spring injection edges (#887) ─────────────────────────────────────────
    if job_id:
        job_manager.update_job(job_id, status_message="Processing Spring injection edges...")
    spring_inject_batch = []
    for fd in all_file_data:
        injections = fd.get("spring_injections")
        if injections:
            spring_inject_batch.extend(injections)
    if spring_inject_batch:
        info_logger(f"[SPRING] Writing {len(spring_inject_batch)} Spring injection edges...")
        await asyncio.to_thread(writer.write_spring_inject_links, spring_inject_batch)

    # Also collect Spring endpoint properties from functions and write them
    endpoint_batch = []
    for fd in all_file_data:
        for fn in fd.get("functions", []):
            if fn.get("http_method"):
                endpoint_batch.append({
                    "func_name": fn["name"],
                    "path": fn["path"],
                    "line_number": fn["line_number"],
                    "http_method": fn.get("http_method"),
                    "http_path": fn.get("http_path"),
                })
    if endpoint_batch:
        await asyncio.to_thread(writer.write_spring_endpoint_properties, endpoint_batch)

    # ── Maven / Gradle build graph (#888) ────────────────────────────────────
    if not is_dependency and path.is_dir():
        if job_id:
            job_manager.update_job(job_id, status_message="Processing Maven build graph...")
        try:
            from ...tools.languages.maven import parse_repo_maven
            maven_data = await asyncio.to_thread(parse_repo_maven, path.resolve())
            if maven_data.get("modules"):
                await asyncio.to_thread(writer.write_maven_build_graph, maven_data, str(path.resolve()))
        except Exception as _me:
            info_logger(f"[MAVEN] Build graph failed (skipping): {_me}")

        if job_id:
            job_manager.update_job(job_id, status_message="Processing Gradle build graph...")
        try:
            from ...tools.languages.gradle import parse_repo_gradle
            gradle_data = await asyncio.to_thread(parse_repo_gradle, path.resolve())
            if gradle_data.get("modules"):
                await asyncio.to_thread(writer.write_gradle_build_graph, gradle_data, str(path.resolve()))
        except Exception as _ge:
            info_logger(f"[GRADLE] Build graph failed (skipping): {_ge}")

    # ── ORM / datasource code linkage (#843) ─────────────────────────────────
    if job_id:
        job_manager.update_job(job_id, status_message="Processing ORM mappings...")
    orm_batch = []
    for fd in all_file_data:
        orm_mappings = fd.get("orm_mappings")
        if orm_mappings:
            orm_batch.extend(orm_mappings)
    if orm_batch:
        class_table_count = sum(1 for r in orm_batch if r.get("kind") == "class_table")
        query_count = sum(1 for r in orm_batch if r.get("kind") == "method_query")
        info_logger(
            f"[ORM] Writing {class_table_count} class→table mappings and {query_count} query links..."
        )
        await asyncio.to_thread(writer.write_orm_mappings, orm_batch)
        await asyncio.to_thread(writer.write_query_links, orm_batch)
        await asyncio.to_thread(writer.write_spring_data_repo_links, orm_batch)

    # ── MyBatis XML mapper READS / WRITES edges ───────────────────────────────
    if not is_dependency and path.is_dir():
        if job_id:
            job_manager.update_job(job_id, status_message="Processing MyBatis XML mappers...")
        try:
            from ...tools.languages.mybatis import find_and_parse_mybatis_mappers
            mybatis_batch = await asyncio.to_thread(find_and_parse_mybatis_mappers, path.resolve(), all_file_data)
            if mybatis_batch:
                await asyncio.to_thread(writer.write_mybatis_links, mybatis_batch)
        except Exception as _me:
            info_logger(f"[MYBATIS] Mapper parsing failed (skipping): {_me}")

    return resolved_calls


async def _vectorize_and_reresolve(ctx: _IndexContext) -> Optional[str]:
    """Embeddings, search vectors, inheritance re-resolution: the closing phases."""
    path = ctx.path
    is_dependency = ctx.is_dependency
    job_id = ctx.job_id
    job_manager = ctx.job_manager
    writer = ctx.writer
    repo_name = ctx.repo_name
    companion_of = ctx.companion_of
    imports_map = ctx.imports_map
    parse_file = ctx.parse_file
    add_minimal_file_node = ctx.add_minimal_file_node
    call_resolution_diagnostics = ctx.call_resolution_diagnostics
    semantic_search = ctx.semantic_search
    companion_roots = ctx.companion_roots
    # ── Phase 4: embedding generation (optional, config-gated) ────────────────
    from ...cli.config_manager import get_config_value as _gcv
    embed_warning: Optional[str] = None
    if (_gcv("ENABLE_VECTOR_RESOLVE") or "false").lower() == "true":
        # Probe first: with no backend installed the pipeline used to fail
        # inside a swallowed except at a log level that is off by default,
        # so 'vector resolve is working' and 'vector resolve has never run'
        # were indistinguishable (#1597).
        from .embeddings import probe_embedding_backend
        _ok, _detail = await asyncio.to_thread(probe_embedding_backend)
        if not _ok:
            embed_warning = f"ENABLE_VECTOR_RESOLVE=true but embeddings were NOT generated: {_detail}"
            error_logger(f"[EMBED] {embed_warning}")
        else:
            if job_id:
                job_manager.update_job(job_id, status_message="Generating embeddings...")
            try:
                from .embeddings import EmbeddingPipeline
                repo_path_str = path.resolve().as_posix()
                info_logger("[EMBED] Starting embedding pipeline...")
                def generate_embeddings():
                    EmbeddingPipeline(writer.driver).run(repo_path_str)

                await asyncio.to_thread(generate_embeddings)
                info_logger("[EMBED] Embedding pipeline complete.")
            except Exception as _ee:
                embed_warning = f"ENABLE_VECTOR_RESOLVE=true but the embedding pipeline failed: {_ee}"
                error_logger(f"[EMBED] {embed_warning}")

    # ── Phase 4b: natural-language search (optional, config-gated) ────────────
    # Keyword texts + vectors on the artifact nodes, ranked by BM25 and
    # LadybugDB's vector index (semantic/graph_index.py).
    try:
        from ...semantic import settings as _ai_settings
        if semantic_search if semantic_search is not None else _ai_settings.enabled():
            if job_id:
                job_manager.update_job(job_id, status_message="Vectorizing artifacts for search...")
            from ...semantic.indexer import sync as _semantic_sync
            # The companion sources are searchable like the repository.
            for _root in [path] + [Path(c) for c in companion_roots]:
                _search = await asyncio.to_thread(
                    _semantic_sync, writer.driver.session, _root.resolve().as_posix()
                )
                if _search.get("error"):
                    warning_logger(f"[SEARCH] {_root.name}: {_search['items']} artifacts indexed, "
                                   f"embeddings skipped: {_search['error']}")
                else:
                    info_logger(f"[SEARCH] {_root.name}: {_search['items']} artifacts, "
                                f"{_search['embedded']} new embeddings")
    except Exception as _se:  # noqa: BLE001 - search must never fail indexing
        warning_logger(f"[SEARCH] search data not updated: {_se}")

    # ── Phase 5: inheritance-aware re-resolution (optional, config-gated) ─────
    if (_gcv("ENABLE_INHERIT_RESOLVE") or "false").lower() == "true":
        if job_id:
            job_manager.update_job(job_id, status_message="Running inheritance re-resolution...")
        try:
            from .resolution.post_resolution import run_inheritance_reresolve
            repo_path_str = path.resolve().as_posix()

            def reresolve_inheritance():
                vector_resolver = None
                if (_gcv("ENABLE_VECTOR_RESOLVE") or "false").lower() == "true":
                    try:
                        from .vector_resolver import VectorResolver
                        vector_resolver = VectorResolver(writer.driver)
                    except Exception as _ve:
                        info_logger(f"[VECTOR] Resolver unavailable: {_ve}")
                return run_inheritance_reresolve(writer.driver, repo_path_str, vector_resolver)

            improved = await asyncio.to_thread(reresolve_inheritance)
            info_logger(f"[INHERIT-RESOLVE] Post-resolution complete: {improved} edges improved")
        except Exception as _ie:
            info_logger(f"[INHERIT-RESOLVE] Post-resolution failed (skipping): {_ie}")

    return embed_warning


async def run_tree_sitter_index_async(
    path: Path,
    is_dependency: bool,
    job_id: Optional[str],
    cvizignore_path: Optional[str],
    writer: GraphWriter,
    job_manager: JobManager,
    parsers: Dict[str, str],
    get_parser: Callable[[str], Any],
    parse_file: Callable[[Path, Path, bool], Dict[str, Any]],
    add_minimal_file_node: Callable[[Path, Path, bool], None],
    call_resolution_diagnostics: Optional[List[Dict[str, Any]]] = None,
    index_summary: Optional[Dict[str, Any]] = None,
    semantic_search: Optional[bool] = None,
    companion_roots: Sequence[Path] = (),
    first_pass_files: Optional[int] = None,
    deferred_out: Optional[Dict[str, Any]] = None,
) -> None:
    """Parse all discovered files, write symbols, then inheritance + CALLS.

    ``first_pass_files`` with ``deferred_out`` makes the index staged: only
    that many files, the highest in priority, are indexed now, and
    ``deferred_out`` receives what run_deferred_index_async needs to index
    the rest later (left empty when nothing is deferred).

    ``semantic_search`` vectorizes the artifacts at the end: None follows
    SEMANTIC_SEARCH, False leaves it to ``cviz graph`` / ``cviz embed``.

    ``companion_roots`` are other repositories or folders indexed in the same
    pass (see core.companions): each file is written under its own
    repository, and calls and inheritance are resolved across all of them.
    """
    if job_id:
        job_manager.update_job(job_id, status=JobStatus.RUNNING)

    repo_root = path if path.is_dir() else path.parent.resolve()
    await asyncio.to_thread(writer.add_repository_to_graph, repo_root, is_dependency)
    repo_name = repo_root.name

    files, _ignore_root = await asyncio.to_thread(
        discover_files_to_index, path, cvizignore_path, supported_extensions=set(parsers.keys())
    )
    main_root = path.resolve() if path.is_dir() else None
    # file -> (repository root, is_dependency) for the files of the companions.
    companion_of: Dict[Path, Tuple[Path, bool]] = {}
    for companion in companion_roots:
        companion = Path(companion).resolve()
        if not companion.is_dir() or companion == main_root:
            continue
        await asyncio.to_thread(writer.add_repository_to_graph, companion, True)
        own_ignore = companion / ".code-visualizer" / ".cvizignore"
        extra, _ = await asyncio.to_thread(
            discover_files_to_index, companion, str(own_ignore) if own_ignore.is_file() else None,
            supported_extensions=set(parsers.keys()),
        )
        info_logger(f"Companion source {companion}: {len(extra)} files")
        for f in extra:
            companion_of[f] = (companion, True)
        files = list(files) + list(extra)

    if job_id:
        job_manager.update_job(job_id, total_files=len(files))

    debug_log("Starting pre-scan to build imports map...")
    imports_map = await asyncio.to_thread(pre_scan_for_imports, files, parsers.keys(), get_parser)
    debug_log(f"Pre-scan complete. Found {len(imports_map)} definitions.")

    ctx = _IndexContext(
        path=path, is_dependency=is_dependency, job_id=job_id, job_manager=job_manager,
        writer=writer, repo_name=repo_name, companion_of=companion_of, imports_map=imports_map,
        parse_file=parse_file, add_minimal_file_node=add_minimal_file_node,
        call_resolution_diagnostics=call_resolution_diagnostics,
        semantic_search=semantic_search, companion_roots=companion_roots,
    )

    # Staged index: the files beyond the first ``first_pass_files`` (in
    # priority order, see prioritize_files) are left to
    # run_deferred_index_async. Every file was pre-scanned above, so the
    # first stage already resolves its calls against the whole repository;
    # the links into deferred files are written by the second stage.
    files_now: List[Path] = list(files)
    if first_pass_files and deferred_out is not None and len(files) > first_pass_files:
        ordered = prioritize_files(files, repo_root)
        files_now, files_later = ordered[:first_pass_files], ordered[first_pass_files:]
        info_logger(
            f"[STAGED] Indexing {len(files_now)} of {len(files)} files now; "
            f"{len(files_later)} deeper or test files are deferred."
        )
        if job_id:
            job_manager.update_job(job_id, total_files=len(files_now))
    else:
        files_later = []

    all_file_data, parse_failures, serialization_start = await _parse_and_write(ctx, files_now)
    resolved_calls = await _link_and_enrich(ctx, all_file_data)

    if files_later:
        embed_warning = None  # vectors wait for the deferred stage
        deferred_out.clear()
        deferred_out.update(
            ctx=dataclasses.replace(ctx, job_id=None),
            files=files_later,
            file_data=all_file_data,
        )
    else:
        embed_warning = await _vectorize_and_reresolve(ctx)

    if index_summary is not None:
        index_summary.clear()
        index_summary.update(
            await asyncio.to_thread(
                build_index_summary,
                files_now,
                parsers,
                all_file_data,
                resolved_calls,
                time.time() - serialization_start,
                parse_failures=parse_failures,
            )
        )
        if embed_warning:
            index_summary.setdefault("warnings", []).append(embed_warning)
        if files_later:
            index_summary["deferred_files"] = len(files_later)

    if job_id:
        job_manager.update_job(job_id, status=JobStatus.COMPLETED, end_time=datetime.now())


async def run_deferred_index_async(
    deferred: Dict[str, Any],
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """Second stage of a staged index (see run_tree_sitter_index_async).

    Parses and writes the deferred files, then resolves the links of the
    whole repository again (first-stage files included: their calls into
    deferred files could not be written before those existed) and runs the
    closing phases (vectors, re-resolution). Returns counts for the log.
    """
    ctx: _IndexContext = deferred["ctx"]
    files: List[Path] = list(deferred["files"])
    new_data, parse_failures, _ = await _parse_and_write(ctx, files, progress=progress)
    all_file_data = sorted(
        list(deferred["file_data"]) + new_data, key=lambda data: str(data.get("path") or "")
    )
    # The first stage resolved its links without the deferred files: a call
    # or a base class defined in one of them went to an external type, or
    # nowhere. MERGE only adds, so those links are removed (as the watcher
    # does before re-linking) and resolved again over the whole repository.
    first_paths = sorted(
        {Path(d["path"]).resolve().as_posix() for d in deferred["file_data"] if d.get("path")}
    )
    if first_paths:
        await asyncio.to_thread(ctx.writer.delete_outgoing_calls_from_files, first_paths)
        await asyncio.to_thread(ctx.writer.delete_inherits_for_files, first_paths)
    await _link_and_enrich(ctx, all_file_data)
    if first_paths:
        await asyncio.to_thread(ctx.writer.delete_orphan_external_classes)
    await _vectorize_and_reresolve(ctx)
    return {"files": len(files), "failed": len(parse_failures)}
