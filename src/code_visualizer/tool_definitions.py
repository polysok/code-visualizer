# src/code_visualizer/tool_definitions.py
"""The MCP tools: a few composable ones over the code graph (see agent_tools).

Modelled on LocAgent (arXiv:2503.09089): find entities by keywords, walk
their dependencies with typed filters, read their code. Indexing, watching
and the repository's settings are the user's business (``cviz graph
--watch``, the graph page), not the assistant's.
"""

_ID_HELP = (
    "Entity ids as the other tools return them: '<path>' for a file, "
    "'<path>:<Class.member>' or '<path>:<Class.member>@<line>' for a function or type "
    "(paths relative to the repository; files of companion sources start with the "
    "companion's name), 'external:<Type>' for a package or DLL type. A bare name "
    "('OrderService.Save') also works when it is unique."
)

TOOLS = {
    "graph_overview": {
        "name": "graph_overview",
        "description": (
            "What the code graph holds: the repository and its companion sources (other "
            "repositories indexed with it), entity counts, the most called packages, and "
            "entry points to start reading from. Call it first when you do not know the code base."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },

    "search_code": {
        "name": "search_code",
        "description": (
            "Find functions, classes and other types by keywords or by a description of what "
            "the code does (e.g. 'retry http request', 'InvoiceService', 'where the space id "
            "header is checked'). Returns entity ids with an excerpt, best first. Start here "
            "to turn an issue or a question into entities."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, identifiers or a short description."},
                "limit": {"type": "integer", "default": 10, "description": "Number of results (1-50)."},
                "detail": {
                    "type": "string", "enum": ["signature", "preview", "full"], "default": "preview",
                    "description": "Excerpt of each result: its first line, its first lines, or all its code.",
                },
            },
            "required": ["query"],
        },
    },

    "traverse_graph": {
        "name": "traverse_graph",
        "description": (
            "Walk the dependencies of entities and get them as a tree: what they call, inherit "
            "or import (downstream) or what calls, derives from or imports them (upstream). "
            "Calls into packages end on external:<Type> nodes with the package and the members "
            "called. Use it to follow a flow, assess the impact of a change, or find where "
            "something is used. " + _ID_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "array", "items": {"type": "string"},
                          "description": "Entity ids to start from (up to 10)."},
                "direction": {
                    "type": "string", "enum": ["downstream", "upstream", "both"], "default": "downstream",
                    "description": "downstream: what they use; upstream: what uses them.",
                },
                "depth": {"type": "integer", "default": 1, "description": "Hops (1-5)."},
                "relations": {
                    "type": "array", "items": {"type": "string", "enum": ["CALLS", "INHERITS", "IMPORTS"]},
                    "default": ["CALLS", "INHERITS"],
                    "description": "Relations to follow. IMPORTS applies to files.",
                },
                "entity_types": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Only show these kinds, e.g. ['Function'] or ['Class', 'Interface', 'ExternalClass'].",
                },
                "include_framework": {
                    "type": "boolean", "default": False,
                    "description": "Also show base-library types (String, Task, ILogger...).",
                },
                "include_tests": {"type": "boolean", "default": True, "description": "Also show test code."},
                "limit_per_node": {"type": "integer", "default": 25,
                                   "description": "Neighbours shown per entity and direction."},
            },
            "required": ["start"],
        },
    },

    "read_code": {
        "name": "read_code",
        "description": (
            "Full code of entities with their file and lines; for a file, the list of what it "
            "declares; for an external type, its package and the members the code calls. "
            + _ID_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}, "description": "Entity ids (up to 10)."},
            },
            "required": ["ids"],
        },
    },

    "execute_cypher_query": {
        "name": "execute_cypher_query",
        "description": (
            "Read-only Cypher query on the code graph, for questions the other tools cannot "
            "answer (counts, multi-step patterns). Nodes: Repository, File, Function, Class, "
            "Interface, Struct, Enum, Record, ExternalClass (name, origin, package, "
            "package_version); properties path, name, line_number, end_line, class_context. "
            "Relations: CONTAINS, CALLS (line_number, called_name), HEURISTIC_CALLS, INHERITS, "
            "IMPLEMENTS, IMPORTS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cypher_query": {"type": "string", "description": "The Cypher query to execute."},
                "params": {"type": "object", "description": "Named parameters of the query.", "default": {}},
            },
            "required": ["cypher_query"],
        },
    },
}
