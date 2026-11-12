# src/code_visualizer/tools/handlers/query_handlers.py
from typing import Any, Dict

class CypherSyntaxError(Exception):
    """Invalid Cypher (raised by callers; LadybugDB reports RuntimeError)."""

from ...utils.cypher_readonly import is_read_only_cypher, read_only_rejection_message
from ...utils.debug_log import debug_log
from ...utils.tool_limits import get_tool_result_limit


def execute_cypher_query(db_manager, **args) -> Dict[str, Any]:
    """
    Tool implementation for executing a read-only Cypher query.

    Write protection is the regex guard, which rejects write keywords before
    the query reaches LadybugDB.
    """
    cypher_query = args.get("cypher_query")
    if not cypher_query:
        return {"error": "Cypher query cannot be empty."}

    params = args.get("params") or args.get("parameters") or {}
    if not isinstance(params, dict):
        return {"error": "Query parameters must be an object/dictionary."}

    if not is_read_only_cypher(cypher_query):
        return {"error": read_only_rejection_message()}

    try:
        debug_log(f"Executing Cypher query: {cypher_query}")
        with db_manager.get_driver().session() as session:
            # The LadybugDB session shim only accepts parameters via **kwargs.
            result = session.run(cypher_query, **params)
            records = [record.data() for record in result]

            limit = get_tool_result_limit("execute_cypher_query")
            truncated = False
            if limit and len(records) > limit:
                records = records[:limit]
                truncated = True

            response = {
                "success": True,
                "query": cypher_query,
                "record_count": len(records),
                "results": records,
            }
            if truncated:
                response["result_limit"] = limit
                response["truncated"] = True
            return response

    except CypherSyntaxError as e:
        debug_log(f"Cypher syntax error: {str(e)}")
        return {
            "error": "Cypher syntax error.",
            "details": str(e),
            "query": cypher_query,
        }
    except Exception as e:
        debug_log(f"Error executing Cypher query: {str(e)}")
        # LadybugDB raises its own exception type for malformed Cypher;
        # surface it as a structured syntax error.
        message = str(e)
        if "syntax" in message.lower() or "parser" in message.lower():
            return {
                "error": "Cypher syntax error.",
                "details": message,
                "query": cypher_query,
            }
        return {
            "error": "An unexpected error occurred while executing the query.",
            "details": message,
        }
