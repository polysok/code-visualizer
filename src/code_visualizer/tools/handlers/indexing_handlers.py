# src/code_visualizer/tools/handlers/indexing_handlers.py
from typing import Any, Dict
from pathlib import Path
import asyncio
from ...utils.debug_log import debug_log
from ...utils.path_sandbox import is_path_allowed as _is_path_allowed
from ...utils.repo_path import repo_record_matches_path


def add_code_to_graph(graph_builder, job_manager, loop, list_repos_func, repo_root=None, **args) -> Dict[str, Any]:
    """
    Tool implementation to index a directory of code.
    Runs indexing asynchronously via a background job.

    Every repository has its own database (``<repo>/.code-visualizer/``):
    a folder outside *repo_root* (the repository of this server's database)
    is only accepted as a dependency of it.
    """
    path = args.get("path") or args.get("repo_path")
    is_dependency = args.get("is_dependency", False)

    if not path:
        return {"error": "Path is a required argument (repo_path)."}

    if repo_root and not is_dependency:
        target = Path(path).resolve().as_posix()
        root = Path(repo_root).resolve().as_posix()
        if target != root and not target.startswith(root.rstrip("/") + "/"):
            return {
                "error": (
                    f"{target} is outside this server's repository ({root}). Each repository "
                    "has its own index: run CViz from that repository, or index it here as a "
                    "dependency (is_dependency: true)."
                ),
            }

    try:
        path_obj = Path(path).resolve()

        # --- Path-traversal guard ---------------------------------------------------
        if not _is_path_allowed(path_obj):
            return {
                "error": (
                    f"Path '{path}' is outside the allowed roots. "
                    "Only subdirectories of the current working directory (or paths "
                    "listed in the CVIZ_ALLOWED_ROOTS environment variable) can be indexed."
                )
            }
        # -----------------------------------------------------------------------------

        if not path_obj.exists():
            return {
                "success": False,
                "status": "path_not_found",
                "error": f"Path '{path}' does not exist.",
                "message": f"Path '{path}' does not exist.",
            }

        # Prevent re-indexing the same repository.
        indexed_repos = list_repos_func().get("repositories", [])
        for repo in indexed_repos:
            if repo_record_matches_path(repo, path_obj):
                return {
                    "success": False,
                    "message": f"Repository '{path}' is already indexed."
                }
        
        # Estimate time and create a job for the user to track.
        total_files, estimated_time = graph_builder.estimate_processing_time(path_obj)
        job_id = job_manager.create_job(str(path_obj), is_dependency)
        job_manager.update_job(job_id, total_files=total_files, estimated_duration=estimated_time)
        
        # Create the coroutine for the background task and schedule it on the main event loop.
        coro = graph_builder.build_graph_from_path_async(
            path_obj, is_dependency, job_id
        )
        asyncio.run_coroutine_threadsafe(coro, loop)
        
        debug_log(f"Started background job {job_id} for path: {str(path_obj)}, is_dependency: {is_dependency}")
        
        return {
            "success": True, "job_id": job_id,
            "message": f"Background processing started for {str(path_obj)}",
            "estimated_files": total_files,
            "estimated_duration_seconds": round(estimated_time, 2),
            "estimated_duration_human": f"{int(estimated_time // 60)}m {int(estimated_time % 60)}s" if estimated_time >= 60 else f"{int(estimated_time)}s",
            "instructions": f"Use 'check_job_status' with job_id '{job_id}' to monitor progress"
        }
    
    except Exception as e:
        debug_log(f"Error creating background job: {str(e)}")
        return {"error": f"Failed to start background processing: {str(e)}"}
