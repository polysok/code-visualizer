from __future__ import annotations
# src/code_visualizer/viz/server.py
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path
import uvicorn
import json
import os
import sys
import re
import requests
import traceback
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from ..cli.open_in_graph import DEFAULT_PORT as DEFAULT_GRAPH_PORT

if TYPE_CHECKING:
    from ..core.database_ladybug import LadybugDBManager as DatabaseManager
from ..utils.debug_log import debug_log
from ..utils.path_sandbox import is_path_allowed
from ..utils.cypher_readonly import is_read_only_cypher as _is_read_only_cypher, read_only_rejection_message
from ..utils import graph_shapes

app = FastAPI()


def _safe_read_text_file(path: str) -> str:
    file_path = Path(path).resolve()
    # Indexed repositories are readable too: the graph page shows the source
    # of any artifact in the graph, wherever the server was started from.
    if not is_path_allowed(file_path) and not graph_api.is_indexed_path(str(file_path)):
        raise HTTPException(status_code=403, detail="File path is outside allowed roots")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()

# CORS: only allow requests from the local visualization frontend.
# The server binds to 127.0.0.1 (localhost) so only local origins are legitimate.
_ALLOWED_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=_ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Standalone dependency-graph page and its API (/graph, /api/locate,
# /api/neighborhood, /api/events, ...). Registered before the SPA fallback
# route below, which would otherwise swallow these paths.
from . import graph_api  # noqa: E402

app.include_router(graph_api.router)
from . import ai_api  # noqa: E402

app.include_router(ai_api.router)

# Global database manager (will be initialized when server starts)
db_manager: Optional[DatabaseManager] = None

def set_db_manager(manager: DatabaseManager):
    global db_manager
    db_manager = manager

def _get_eid(element):
    """Stable string id of a LadybugDB node / relationship dict or raw id.

    LadybugDB internal ids are ``{'table': T, 'offset': O}`` dicts, found
    under ``_ID`` (``_id`` in older Kùzu-style results) or as the bare
    ``_SRC`` / ``_DST`` endpoint values.
    """
    if element is None:
        return None
    if isinstance(element, (int, str)):
        return str(element)
    if isinstance(element, dict):
        if 'offset' in element and 'table' in element:
            return f"{element.get('table')}_{element.get('offset')}"
        val = graph_shapes.meta(element, '_id')
        if val is None:
            val = element.get('id')
        if val is not None:
            if isinstance(val, dict):
                return f"{val.get('table', 0)}_{val.get('offset', 0)}"
            return str(val)
    return str(id(element))


def _meta(d: dict, name: str):
    """Read a LadybugDB internal field (``_LABEL``, ``_SRC``…) in either casing."""
    return graph_shapes.meta(d, f"_{name}")


@app.get("/api/graph")
async def get_graph(repo_path: Optional[str] = None, cypher_query: Optional[str] = None):
    if not db_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    try:
        nodes_dict = {}
        edges = []

        print(f"DEBUG: Starting get_graph with repo_path={repo_path}", flush=True)

        # This endpoint only ever reads; user queries pass the read-only guard.
        with db_manager.get_driver().session() as session:
            if cypher_query:
                if not _is_read_only_cypher(cypher_query):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "This endpoint only supports read-only Cypher queries. "
                            "Prohibited keywords like CREATE, MERGE, DELETE, SET, REMOVE, "
                            "DROP, or CALL apoc are not allowed."
                        ),
                    )
                print(f"DEBUG: Executing custom query: {cypher_query}", flush=True)
                result = session.run(cypher_query)
            elif repo_path:
                repo_path = str(Path(repo_path).resolve())
                print(f"DEBUG: Fetching subgraph for: {repo_path}", flush=True)
                # Get all nodes within the repository scope
                query = """
                MATCH (r:Repository {path: $repo_path})
                OPTIONAL MATCH (r)-[:CONTAINS*0..]->(n)
                WITH DISTINCT r, COLLECT(DISTINCT n) as repo_nodes
                UNWIND repo_nodes as node
                OPTIONAL MATCH (node)-[rel]->(target)
                WITH r, node, rel, target, repo_nodes
                WHERE target IN repo_nodes OR target = r
                RETURN node as n, rel, target as m
                """
                result = session.run(query, repo_path=repo_path)
            else:
                print("DEBUG: Fetching global graph", flush=True)
                query = "MATCH (n) OPTIONAL MATCH (n)-[rel]->(m) RETURN n, rel, m LIMIT 50000"
                result = session.run(query)

            record_count = 0
            for record in result:
                record_count += 1
                # .get(): a custom query need not return all of n, rel, m.
                for key in ['n', 'm']:
                    node = record.get(key)
                    if isinstance(node, dict):
                        try:
                            parse_node(node, nodes_dict)
                        except Exception as e:
                            print(f"DEBUG: Error parsing node: {e}", file=sys.stderr, flush=True)
                rel = record.get('rel')
                if isinstance(rel, dict):
                    try:
                        parse_rel(rel, edges)
                    except Exception as e:
                        print(f"DEBUG: Error parsing relationship: {e}", file=sys.stderr, flush=True)

        print(f"DEBUG: Processed {record_count} records. extracted {len(nodes_dict)} nodes and {len(edges)} edges.", file=sys.stderr, flush=True)

        # Build a list of unique file paths from File-type nodes for the tree
        file_paths = []
        for n in nodes_dict.values():
            if n.get("file") and str(n.get("type", "")).lower() == "file":
                file_paths.append(str(n["file"]))
        file_paths = sorted(list(set(file_paths)))

        # Read file contents so the frontend never needs a separate API call
        file_contents: dict[str, str] = {}
        for fp in file_paths:
            try:
                resolved = Path(fp).resolve()
                if not is_path_allowed(resolved):
                    continue
                file_contents[fp] = _safe_read_text_file(str(resolved))
            except HTTPException:
                continue
            except Exception:
                pass

        response_data = {
            "nodes": list(nodes_dict.values()), 
            "links": edges,
            "files": file_paths,
            "fileContents": file_contents,
        }
        
        print(f"API SUCCESS: Returning graph with {len(response_data['nodes'])} nodes and {len(response_data['links'])} links.", file=sys.stderr, flush=True)
        return response_data

    except HTTPException:
        # Preserve intentional HTTP errors (e.g. 400 for forbidden write queries).
        raise
    except Exception as e:
        debug_log(f"Error fetching graph: {str(e)}")
        import traceback
        traceback.print_exc()
        # Still return a valid structure so the frontend doesn't crash, but with 500 status if raised
        # Actually, let's just return a 500 error but with JSON body if possible
        raise HTTPException(status_code=500, detail=str(e))

def call_llm(system_prompt: str, user_prompt: str) -> str:
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    atlascloud_key = os.getenv("ATLASCLOUD_API_KEY") or os.getenv("ATLAS_CLOUD_API_KEY")
    cheaperinference_key = os.getenv("CHEAPER_INFERENCE_API_KEY")
    api_route_key = os.getenv("API_ROUTE_API_KEY")
    
    if not gemini_key and not openai_key and not atlascloud_key and not cheaperinference_key and not api_route_key:
        raise ValueError(
            "GEMINI_API_KEY, OPENAI_API_KEY, ATLASCLOUD_API_KEY, CHEAPER_INFERENCE_API_KEY, or API_ROUTE_API_KEY "
            "environment variable is required."
        )
    
    if gemini_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"{system_prompt}\n\nUser Prompt:\n{user_prompt}"}]
                }
            ]
        }
        res = requests.post(url, json=payload, timeout=30)
        if res.status_code != 200:
            raise RuntimeError(f"Gemini API returned error {res.status_code}: {res.text}")
        data = res.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Failed to parse Gemini API response: {data}") from e
    else:
        if openai_key:
            provider_name = "OpenAI"
            api_key = openai_key
            url = "https://api.openai.com/v1/chat/completions"
            model = "gpt-4o-mini"
        elif atlascloud_key:
            provider_name = "Atlas Cloud"
            api_key = atlascloud_key
            base_url = os.getenv("ATLASCLOUD_API_BASE") or os.getenv("ATLAS_CLOUD_API_BASE")
            base_url = (base_url or "https://api.atlascloud.ai/v1").rstrip("/")
            url = f"{base_url}/chat/completions"
            model = os.getenv("ATLASCLOUD_MODEL") or os.getenv("ATLAS_CLOUD_MODEL")
            model = model or "deepseek-ai/deepseek-v4-pro"
        elif cheaperinference_key:
            provider_name = "Cheaper Inference"
            api_key = cheaperinference_key
            base_url = os.getenv("CHEAPER_INFERENCE_API_BASE")
            base_url = (base_url or "https://api.cheaperinference.com/v1").rstrip("/")
            url = f"{base_url}/chat/completions"
            model = os.getenv("CHEAPER_INFERENCE_MODEL") or "gpt-5.4-mini"
        else:
            provider_name = "API Route"
            api_key = api_route_key
            base_url = os.getenv("API_ROUTE_API_BASE")
            base_url = (base_url or "https://global.api-route.com/v1").rstrip("/")
            url = f"{base_url}/chat/completions"
            model = os.getenv("API_ROUTE_MODEL") or "gpt-6.1-sol"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        }
        res = requests.post(url, json=payload, headers=headers, timeout=30)
        if res.status_code != 200:
            raise RuntimeError(f"{provider_name} API returned error {res.status_code}: {res.text}")
        data = res.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Failed to parse {provider_name} API response: {data}") from e


def parse_json_from_llm(text: str) -> dict:
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1)
    else:
        match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            text = match.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse valid JSON from LLM response:\n{text}")


def parse_node(node, nodes_dict):
    """Add a LadybugDB node dict to ``nodes_dict`` (keyed by its id)."""
    eid = _get_eid(node)
    if not eid or eid in nodes_dict:
        return

    meta_label = _meta(node, 'label')
    if meta_label is not None:
        labels = [meta_label]
    elif 'label' in node:
        labels = [node['label']]
    else:
        labels = []
    props = graph_shapes.public_properties(node)

    display_name = str(props.get('name', props.get('label', props.get('path', 'Unknown'))))
    if display_name == "<module>":
        _fpath = str(props.get('path', props.get('file', '')))
        if _fpath:
            # Same plain-filename label as the payload builders in
            # cli_helpers/visualize_graph, so one node cannot render two
            # different names in different views.
            display_name = os.path.basename(_fpath)
            props['name'] = display_name
            props['label'] = display_name

    nodes_dict[eid] = {
        "id": eid,
        "name": display_name,
        "label": display_name,
        "type": str(labels[0]).capitalize() if labels else "Other",
        "file": str(props.get('path', props.get('file', ''))),
        "val": 4 if (labels and labels[0] in ['Repository', 'Class', 'Interface', 'Trait']) else 2,
        "properties": props
    }


def parse_rel(rel, edges):
    """Append a LadybugDB relationship dict (``_SRC`` / ``_DST``) to ``edges``."""
    src = _meta(rel, 'src')
    dst = _meta(rel, 'dst')
    source = _get_eid(src) if src is not None else None
    target = _get_eid(dst) if dst is not None else None
    if source and target:
        edges.append({
            "id": _get_eid(rel),
            "source": source,
            "target": target,
            "type": str(_meta(rel, 'label') or rel.get('type', 'RELATED')).upper(),
        })


def parse_element(val, nodes_dict, edges):
    if val is None:
        return
    # Classification is shared with the offline renderer (utils/graph_shapes).
    if graph_shapes.is_node(val):
        parse_node(val, nodes_dict)
    elif graph_shapes.is_relationship(val):
        parse_rel(val, edges)
    elif isinstance(val, (list, tuple)):
        for item in val:
            parse_element(item, nodes_dict, edges)


class AIQueryRequest(BaseModel):
    query: str
    repo_path: Optional[str] = None


@app.post("/api/ai_query")
async def ai_query(request: AIQueryRequest):
    if not db_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")
        
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    atlascloud_key = os.getenv("ATLASCLOUD_API_KEY") or os.getenv("ATLAS_CLOUD_API_KEY")
    cheaperinference_key = os.getenv("CHEAPER_INFERENCE_API_KEY")
    api_route_key = os.getenv("API_ROUTE_API_KEY")
    
    if not gemini_key and not openai_key and not atlascloud_key and not cheaperinference_key and not api_route_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "AI querying requires GEMINI_API_KEY, OPENAI_API_KEY, ATLASCLOUD_API_KEY, "
                "CHEAPER_INFERENCE_API_KEY, or API_ROUTE_API_KEY. "
                "Please set one in your configuration or .env."
            )
        )
        
    query = request.query
    repo_path = request.repo_path
    
    translation_prompt = """You are an expert graph database administrator. Translate the user's natural language question about a codebase into a valid read-only Cypher query.

The graph database represents the codebase structure with the following schema:
### Nodes & Properties:
* **`Repository`**: `name` (string), `path` (string, absolute path), `is_dependency` (boolean)
* **`File`**: `name` (string), `path` (string, absolute path), `relative_path` (string), `is_dependency` (boolean)
* **`Function`**: `name` (string), `path` (string, absolute path), `line_number` (int), `end_line` (int), `args` (list of strings), `cyclomatic_complexity` (int), `decorators` (list of strings), `lang` (string), `source` (string), `is_dependency` (boolean)
* **`Class`**: `name` (string), `path` (string, absolute path), `line_number` (int), `end_line` (int), `bases` (list of strings), `decorators` (list of strings), `lang` (string), `source` (string), `is_dependency` (boolean)
* **`Interface`**, **`Struct`**, **`Enum`**, **`Trait`**: similar properties to Class/Function
* **`Module`**: `name` (string)

### Relationships:
* **`CONTAINS`**: (Repository)-[:CONTAINS]->(File), (File)-[:CONTAINS]->(Function), (File)-[:CONTAINS]->(Class)
* **`CALLS`**: (Function)-[:CALLS]->(Function)
* **`IMPORTS`**: (File)-[:IMPORTS]->(Module)
* **`INHERITS`**: (Class)-[:INHERITS]->(Class)
* **`IMPLEMENTS`**: (Class)-[:IMPLEMENTS]->(Interface)

### Query Writing Rules:
1. Only write SELECT/MATCH read-only queries. Do NOT use CREATE, MERGE, DELETE, SET, REMOVE, or other modifying keywords.
2. Return nodes using the variable names `n` and `m`, and relationships using `rel` where possible. This is critical for the visualizer.
   Example: MATCH (n:Class)-[rel:INHERITS]->(m:Class) RETURN n, rel, m
3. Keep the query simple and efficient. Limit results using `LIMIT 150` unless more are needed.
4. If the user asks about dependencies or paths, try to return paths using variables, and return nodes/relationships.
5. Make property matches case-insensitive or fuzzy if appropriate (e.g. using `toLower(n.name) CONTAINS 'auth'` or `n.name =~ '(?i).*auth.*'`).
6. If the user is querying a specific repository, you can filter by `Repository.path` or `Repository.name` if path is supplied.

Respond ONLY with a JSON object in this format:
{
  "cypher_query": "MATCH ... RETURN n, rel, m LIMIT 100",
  "explanation": "Brief description of what this Cypher query does"
}
"""
    
    try:
        # Call LLM to get Cypher
        llm_response = call_llm(translation_prompt, query)
        parsed_response = parse_json_from_llm(llm_response)
        cypher_query = parsed_response.get("cypher_query", "")
        translation_explanation = parsed_response.get("explanation", "")
        
        if not cypher_query:
            raise ValueError("No Cypher query was generated by the AI.")
            
        # Validate Cypher
        if not _is_read_only_cypher(cypher_query):
            raise HTTPException(
                status_code=400,
                detail=f"The generated Cypher query was rejected for safety reasons: {read_only_rejection_message()}"
            )
            
        # Step 2: Run Cypher query
        nodes_dict = {}
        edges = []
        records_data = []

        with db_manager.get_driver().session() as session:
            result = session.run(cypher_query)
            for record in result:
                # Store truncated record data for explanation phase
                record_dict = {}
                for k, v in record.items():
                    if isinstance(v, dict):
                        props = graph_shapes.public_properties(v)
                        record_dict[k] = {prop_k: (prop_v[:200] + "..." if isinstance(prop_v, str) and len(prop_v) > 200 else prop_v) for prop_k, prop_v in props.items()}
                    else:
                        record_dict[k] = str(v)[:200] + "..." if isinstance(v, str) and len(v) > 200 else v
                records_data.append(record_dict)
                
                # Parse elements for visualization
                for val in record.values():
                    parse_element(val, nodes_dict, edges)
                    
        # Truncate results for LLM context (e.g. limit to first 30 records to save tokens)
        results_for_explanation = records_data[:30]
        results_json = json.dumps(results_for_explanation, indent=2)
        
        # Step 3: Generate Explanation
        explanation_prompt = f"""You are an expert software developer and repository assistant. Explain the answer to the user's question about the codebase based on the query results.

User Question: {query}
Cypher Query Executed: {cypher_query}
Query Results (truncated if large):
{results_json}

Write a clear, concise, and professional explanation of the results in Markdown format.
Focus on explaining:
1. What the query found.
2. The architectural relationships between the components.
3. How this answers the user's question.

After your explanation, provide a section:
### Context-Aware Recommendations
Suggest 2-3 specific follow-up questions or related areas the user might want to explore based on this result. Keep suggestions bulleted and actionable.
"""
        explanation = call_llm(explanation_prompt, "Explain the results.")
        
        return {
            "success": True,
            "cypher_query": cypher_query,
            "translation_explanation": translation_explanation,
            "explanation": explanation,
            "nodes": list(nodes_dict.values()),
            "links": edges
        }
        
    except HTTPException:
        # Preserve intentional HTTP errors (e.g. the 400 raised above when the
        # generated Cypher is not read-only); re-wrapping them as 500 made a
        # safety rejection look like a server failure to the frontend.
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/file")
def get_file(request: Request, path: str):
    graph_api.guard(request)
    try:
        return {"content": _safe_read_text_file(path)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# The graph page is the only UI: the root goes there, anything else is unknown.
@app.get("/{full_path:path}")
async def fallback(request: Request, full_path: str):
    if full_path == "":
        return RedirectResponse(url="/graph")
    return HTMLResponse("Not Found", status_code=404)

class _GraphServer(uvicorn.Server):
    """Ends the open pages' SSE streams as soon as Ctrl+C is pressed.

    Uvicorn waits for every connection to close before it stops; the live
    event streams of open graph tabs never close on their own, so the server
    used to hang on "Waiting for connections to close".
    """

    def handle_exit(self, sig, frame):
        from .graph_api import broker

        broker.close()
        super().handle_exit(sig, frame)


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_GRAPH_PORT):
    # timeout_graceful_shutdown: a safety net for any other long request.
    config = uvicorn.Config(app, host=host, port=port, timeout_graceful_shutdown=3)
    _GraphServer(config).run()
