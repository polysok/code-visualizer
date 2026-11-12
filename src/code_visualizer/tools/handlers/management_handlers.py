# src/code_visualizer/tools/handlers/management_handlers.py
from typing import Any, Dict
from ...utils.debug_log import debug_log
from ..code_finder import CodeFinder

def list_indexed_repositories(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to list indexed repositories."""
    try:
        debug_log("Listing indexed repositories.")
        results = code_finder.list_indexed_repositories()
        return {
            "success": True,
            "repositories": results
        }
    except Exception as e:
        debug_log(f"Error listing indexed repositories: {str(e)}")
        return {"error": f"Failed to list indexed repositories: {str(e)}"}
