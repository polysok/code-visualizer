"""Code-graph tools for an AI agent: search, traverse, read, overview.

Modelled on LocAgent (Chen et al., 2025, "LocAgent: Graph-Guided LLM Agents
for Code Localization", arXiv:2503.09089): a few composable tools over one
graph beat a long list of special-purpose queries. An agent finds entry
entities by keywords (``search_code``), walks their dependencies with typed
filters (``traverse_graph``) and reads what it needs (``read_code``).

Entities are named by stable, readable ids: the path inside the main
repository, or prefixed with the companion source's name for its files:

* ``Services/Svc.cs``                     a file
* ``Services/Svc.cs:Svc.Run``             a function or type (class-qualified)
* ``Services/Svc.cs:Svc.Run@42``          the same, the line telling overloads apart
* ``Acme.Billing/InvoiceCalc.cs:InvoiceCalc.Total``   in a companion source
* ``external:ILogger``                    a type from a package or DLL

Ids from an earlier call stay valid after an edit: the line is only a hint,
the nearest definition with that name wins. Results are plain text, trees
for the walks, because that is what a language model reads best.

Every function takes a database session; the MCP server calls them on its
own database or, when ``cviz graph`` already holds it, through the graph
server (``/api/agent/*``).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .viz import neighborhood as nb
from .viz.neighborhood import ARTIFACT_LABELS, MODULE_PSEUDO_FUNCTION, PathMapper, _rows, _to_int

EXTERNAL_PREFIX = "external:"
DETAILS = ("signature", "preview", "full")
DIRECTIONS = {
    # what the entity uses / what uses it (LocAgent's downstream / upstream)
    "downstream": ("callee", "parent", "import"),
    "upstream": ("caller", "child"),
    "both": ("callee", "parent", "import", "caller", "child"),
}
RELATIONS = ("CALLS", "INHERITS", "IMPORTS")
PREVIEW_LINES = 8
MAX_READ_LINES = 400
_ROLE_LABEL = {
    ("CALLS", "callee"): "calls →", ("CALLS", "caller"): "called by ←",
    ("INHERITS", "parent"): "inherits from →", ("INHERITS", "child"): "inherited by ←",
    ("IMPORTS", "import"): "imports →",
}


class AgentToolError(ValueError):
    """A request the tools cannot answer; the message tells the agent what to do."""


# ---------------------------------------------------------------------------
# Repositories and entity ids
# ---------------------------------------------------------------------------


class _Repos:
    """Repositories of the database (the main one and its companion sources)."""

    def __init__(self, session):
        rows = _rows(session, "MATCH (r:Repository) RETURN r.name AS name, r.path AS path, "
                              "r.is_dependency AS dep")
        self.rows = sorted(
            ({"name": r["name"], "path": nb._norm(r["path"]).rstrip("/"), "companion": bool(r.get("dep"))}
             for r in rows if r.get("path")),
            key=lambda r: -len(r["path"]),
        )

    def split(self, path: str) -> Tuple[Optional[str], str]:
        """``/abs/App/a/b.cs`` -> (``App``, ``a/b.cs``)."""
        p = nb._norm(path or "")
        for repo in self.rows:
            if p == repo["path"] or p.startswith(repo["path"] + "/"):
                return repo["name"], p[len(repo["path"]):].lstrip("/")
        return None, p

    @property
    def main(self) -> Optional[Dict[str, Any]]:
        return next((r for r in self.rows if not r["companion"]), self.rows[0] if self.rows else None)

    def companion_root(self, name: str) -> Optional[str]:
        for repo in self.rows:
            if repo["companion"] and repo["name"] == name:
                return repo["path"]
        return None

    def rel(self, path: str) -> str:
        """Main repository files without prefix, companion files under their name."""
        name, rel = self.split(path)
        main = self.main
        if name and main and name == main["name"] and nb._norm(path).startswith(main["path"] + "/"):
            return rel
        return f"{name}/{rel}" if name else rel


def _qualified(name: str, class_context: Optional[str]) -> str:
    return f"{class_context}.{name}" if class_context and class_context != name else name


def entity_id(repos: _Repos, kind: str, path: Optional[str], name: Optional[str],
              line: Optional[int] = None, class_context: Optional[str] = None, with_line: bool = True) -> str:
    if kind == "ExternalClass" or not path:
        return f"{EXTERNAL_PREFIX}{name}"
    file_part = repos.rel(path)
    if kind == "File":
        return file_part
    qualified = _qualified(name or "?", class_context)
    return f"{file_part}:{qualified}" + (f"@{line}" if with_line and line else "")


_ID = re.compile(r"^(?P<file>[^:@]+?)(?::(?P<name>[^@]+?))?(?:@(?P<line>\d+))?$")


def _class_contexts(session, nodes: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str, Optional[int]], str]:
    """class_context of the Function nodes among *nodes*, by (path, name, line)."""
    funcs = [n for n in nodes if n.get("kind") == "Function" and n.get("indexed_path")]
    if not funcs:
        return {}
    rows = _rows(
        session,
        "MATCH (n:Function) WHERE n.path IN $paths AND n.name IN $names "
        "RETURN n.path AS path, n.name AS name, n.line_number AS line, n.class_context AS cls",
        paths=sorted({nb._norm(n["indexed_path"]) for n in funcs}),
        names=sorted({n["name"] for n in funcs if n.get("name")}),
    )
    return {nb._key(r["path"], r["name"], r["line"]): r["cls"] for r in rows if r.get("cls")}


def resolve(session, repos: _Repos, ref: str) -> Dict[str, Any]:
    """The graph artifact an id (or a bare symbol name) designates."""
    ref = (ref or "").strip()
    if not ref:
        raise AgentToolError("Empty entity id.")
    if ref.startswith(EXTERNAL_PREFIX):
        name = ref[len(EXTERNAL_PREFIX):]
        rows = _rows(session, "MATCH (e:ExternalClass {name: $name}) RETURN e.name AS name", name=name)
        if not rows:
            raise AgentToolError(f"No external type named {name} in the graph.")
        return {"kind": "ExternalClass", "name": name, "path": None, "line": None}

    if "/" not in ref and ":" not in ref:
        return _by_name(session, repos, ref.split("@", 1)[0])  # a bare (Class.)member name
    m = _ID.match(ref)
    if not m:
        raise AgentToolError(f"Not an entity id: {ref}")
    file_part, name, line = m.group("file"), m.group("name"), _to_int(m.group("line"))
    path = _absolute(repos, file_part)
    if path is None:
        raise AgentToolError("Nothing is indexed yet.")
    if name is None:
        rows = _rows(session, "MATCH (f:File {path: $path}) RETURN f.path AS path", path=path)
        if not rows:
            raise AgentToolError(f"File {file_part} is not in the graph.")
        return {"kind": "File", "name": Path(path).name, "path": path, "line": None}

    simple = name.rsplit(".", 1)[-1]
    owner = name.rsplit(".", 1)[0] if "." in name else None
    found = []
    for label in ARTIFACT_LABELS:
        for row in _rows(session, f"MATCH (n:{label}) WHERE n.path = $path AND n.name = $name "
                                  "RETURN n.name AS name, n.path AS path, n.line_number AS line, "
                                  "n.end_line AS end_line"
                                  + (", n.class_context AS cls" if label == "Function" else ""),
                         path=path, name=simple):
            if owner and label == "Function" and row.get("cls") and row["cls"] != owner:
                continue
            found.append({"kind": label, **row})
    if not found:
        raise AgentToolError(f"No {name} in {file_part}: search_code finds the current ids.")
    found.sort(key=lambda r: (abs((_to_int(r.get("line")) or 0) - (line or 0)) if line else 0,
                              ARTIFACT_LABELS.index(r["kind"])))
    return found[0]


def _absolute(repos: _Repos, file_part: str) -> Optional[str]:
    if Path(file_part).is_absolute():
        return nb._norm(file_part)
    head, _, rest = file_part.partition("/")
    root = repos.companion_root(head)
    if root:
        return f"{root}/{rest}" if rest else root
    main = repos.main
    return f"{main['path']}/{file_part}" if main else None


def _by_name(session, repos: _Repos, name: str) -> Dict[str, Any]:
    owner, _, simple = name.rpartition(".")
    matches = nb.resolve_symbol(session, simple)
    if owner and matches:
        classes = _class_contexts(session, matches)
        matches = [m for m in matches if m["kind"] != "Function"
                   or classes.get(nb._key(m["indexed_path"], m["name"], m["line"])) == owner] or matches
    if len(matches) == 1:
        m = matches[0]
        return {"kind": m["kind"], "name": m["name"], "path": m["indexed_path"], "line": m["line"],
                "end_line": m["end_line"]}
    if not matches:
        raise AgentToolError(f"Nothing named {name}: use search_code to find the entity and its id.")
    classes = _class_contexts(session, matches[:15])
    listed = "\n".join(
        f"  {entity_id(repos, m['kind'], m['indexed_path'], m['name'], m['line'], classes.get(nb._key(m['indexed_path'], m['name'], m['line'])))}"
        f"  [{m['kind']}]" for m in matches[:15])
    raise AgentToolError(f"{len(matches)} entities are named {name}; pass one of these ids:\n{listed}")


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------


def _source_lines(path: str, start: Optional[int], end: Optional[int], limit: int) -> List[str]:
    if not start:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    stop = min(len(lines), end or start, start + limit - 1)
    return lines[start - 1:stop]


def _excerpt(path: str, line: Optional[int], end: Optional[int], detail: str) -> str:
    if detail == "signature":
        lines = _source_lines(path, line, line, 1)
    elif detail == "preview":
        lines = _source_lines(path, line, end, PREVIEW_LINES)
        if end and line and end - line + 1 > PREVIEW_LINES:
            lines.append(f"    … ({end - line + 1 - PREVIEW_LINES} more lines: read_code)")
    else:
        lines = _source_lines(path, line, end, MAX_READ_LINES)
    return "\n".join(lines)


def _name_matches(session, query: str, limit: int) -> List[Dict[str, Any]]:
    """Fallback when no search index exists: names containing a query word."""
    words = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)] or [query]
    out: Dict[Tuple, Dict[str, Any]] = {}
    for word in words[:6]:
        for label in ARTIFACT_LABELS:
            for row in _rows(session, f"MATCH (n:{label}) WHERE lower(n.name) CONTAINS $w "
                                      "RETURN n.name AS name, n.path AS path, n.line_number AS line, "
                                      "n.end_line AS end_line LIMIT 50", w=word.lower()):
                if row.get("name") == MODULE_PSEUDO_FUNCTION:
                    continue
                exact = (row["name"] or "").lower() == word.lower()
                key = (row["path"], row["name"], row["line"])
                entry = out.setdefault(key, {"kind": label, **row, "score": 0})
                entry["score"] += 3 if exact else 1
    return sorted(out.values(), key=lambda r: -r["score"])[:limit]


def search_code(session, query: str, limit: int = 10, detail: str = "preview",
                mapper: Optional[PathMapper] = None, engine=None) -> str:
    """Entities matching keywords or a description, best first."""
    mapper = mapper or PathMapper()
    detail = detail if detail in DETAILS else "preview"
    limit = max(1, min(int(limit or 10), 50))
    repos = _Repos(session)
    note = ""
    hits: List[Dict[str, Any]] = []
    try:
        from .semantic.search import SearchEngine

        result = (engine or SearchEngine(lambda: _NoClose(session))).search(query, limit=limit)
        hits = [{"kind": h.item.kind, "name": h.item.name, "path": h.item.path, "line": h.item.line,
                 "end_line": h.item.end_line} for h in result["hits"]]
        if result["mode"] == "keyword":
            note = "keyword search only (no vectors yet)"
        elif result["mode"] == "none":
            hits, note = [], ""
    except Exception:  # noqa: BLE001 - fall back to names
        hits = []
    if not hits:
        hits = _name_matches(session, query, limit)
        note = "matched on names only: the search index is not built (cviz graph builds it)"
    if not hits:
        return f"No entity matches {query!r}. Try other words, or a class or method name."

    nodes = [{"kind": h["kind"], "name": h["name"], "indexed_path": h["path"], "line": h["line"]} for h in hits]
    classes = _class_contexts(session, nodes)
    out = [f"{len(hits)} result(s) for {query!r}" + (f" ({note})" if note else "") + ":"]
    for h in hits:
        cls = classes.get(nb._key(h["path"], h["name"], h["line"]))
        out.append("")
        out.append(f"{entity_id(repos, h['kind'], h['path'], h['name'], h['line'], cls)}  [{h['kind']}]"
                   f"  lines {h['line']}-{h.get('end_line') or h['line']}")
        text = _excerpt(mapper.to_local(h["path"]) or h["path"], h["line"], h.get("end_line"), detail)
        if text:
            out.extend("    " + line for line in text.splitlines())
    return "\n".join(out)


class _NoClose:
    """Lend an open session to code that closes what it opens."""

    def __init__(self, session):
        self._s = session

    def __enter__(self):
        return self._s

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return getattr(self._s, name)


# ---------------------------------------------------------------------------
# traverse_graph
# ---------------------------------------------------------------------------


def _origin_text(node: Dict[str, Any]) -> str:
    origin = node.get("origin")
    if origin == "framework":
        return ".NET framework"
    if origin == "package":
        pkg = f"NuGet {node.get('package')}" + (f" {node['package_version']}" if node.get("package_version") else "")
        return pkg + (" (probable)" if node.get("evidence") == "usings" else "")
    if origin == "ambiguous":
        return "one of " + ", ".join(node.get("candidates") or [])
    return "package or DLL"


def traverse_graph(session, start: Sequence[str], direction: str = "downstream", depth: int = 1,
                   relations: Sequence[str] = ("CALLS", "INHERITS"),
                   entity_types: Optional[Sequence[str]] = None,
                   include_framework: bool = False, include_tests: bool = True,
                   limit_per_node: int = 25, mapper: Optional[PathMapper] = None) -> str:
    """Dependency tree around each start entity."""
    mapper = mapper or PathMapper()
    if isinstance(start, str):
        start = [start]
    if not start:
        raise AgentToolError("Give at least one start entity id.")
    if direction not in DIRECTIONS:
        raise AgentToolError(f"direction must be one of {', '.join(DIRECTIONS)}.")
    relations = [r.upper() for r in (relations or RELATIONS) if r.upper() in RELATIONS] or ["CALLS"]
    types = {t.lower() for t in entity_types} if entity_types else None
    depth = max(1, min(int(depth or 1), nb.MAX_DEPTH))
    repos = _Repos(session)
    blocks = []
    for ref in start[:10]:
        target = resolve(session, repos, ref)
        if target["kind"] == "ExternalClass":
            blocks.append(_external_callers(session, repos, target["name"], limit_per_node))
            continue
        data = nb.neighborhood(
            session, path=target["path"], name=None if target["kind"] == "File" else target["name"],
            line=target.get("line"), kind=target["kind"], depth=depth, kinds=relations,
            max_nodes=400, per_node_limit=max(1, min(int(limit_per_node or 25), 200)), mapper=mapper,
            hide_framework=not include_framework, hide_tests=not include_tests,
            roles=DIRECTIONS[direction],
        )
        blocks.append(_tree(session, repos, data, types))
    return "\n\n".join(blocks)


def _label(repos: _Repos, node: Dict[str, Any], classes: Dict) -> str:
    cls = classes.get(nb._key(node.get("indexed_path"), node.get("name"), node.get("line")))
    ident = entity_id(repos, node["kind"], node.get("indexed_path"), node.get("name"), node.get("line"), cls)
    extra = f" · {_origin_text(node)}" if node["kind"] == "ExternalClass" else ""
    return f"{ident}  [{node['kind']}{extra}]"


def _tree(session, repos: _Repos, data: Dict[str, Any], types: Optional[set]) -> str:
    nodes = {n["id"]: n for n in data["nodes"]}
    classes = _class_contexts(session, nodes.values())
    children: Dict[str, List[Tuple[str, Dict[str, Any], str]]] = {}
    for link in data["links"]:
        if link["type"] == "CONTAINS":
            continue
        s, t = nodes.get(link["source"]), nodes.get(link["target"])
        if not s or not t or s["id"] == t["id"]:
            continue
        if t["role"] in ("callee", "parent", "import") and t["depth"] == s["depth"] + 1:
            parent, child = s, t
        elif s["role"] in ("caller", "child") and s["depth"] == t["depth"] + 1:
            parent, child = t, s
        else:
            continue
        children.setdefault(parent["id"], []).append((child["id"], link, child["role"]))

    center = data["center"]["id"]
    lines = [_label(repos, nodes[center], classes)]
    seen = {center}

    def walk(nid: str, prefix: str) -> None:
        kids = [k for k in children.get(nid, [])
                if types is None or nodes[k[0]]["kind"].lower() in types]
        kids.sort(key=lambda k: (k[2] not in ("callee", "parent", "import"), k[1].get("line") or 0))
        hidden = nodes[nid].get("hidden") or {}
        for i, (cid, link, role) in enumerate(kids):
            last = i == len(kids) - 1 and not hidden
            node = nodes[cid]
            rel = _ROLE_LABEL.get((link["type"], role), link["type"].lower())
            tags = []
            if link.get("heuristic"):
                tags.append("by name")
            if link.get("members"):
                tags.append("." + ", .".join(link["members"][:6]))
            if link.get("lines") and role in ("callee",):
                tags.append("line " + ", ".join(str(x) for x in link["lines"][:4]))
            again = cid in seen
            text = f"{rel} {_label(repos, node, classes)}" + (f"  ({'; '.join(tags)})" if tags else "")
            lines.append(f"{prefix}{'└── ' if last else '├── '}{text}" + ("  (see above)" if again else ""))
            if not again:
                seen.add(cid)
                walk(cid, prefix + ("    " if last else "│   "))
        if hidden:
            more = ", ".join(f"{v} more {k}{'s' if v > 1 else ''}" for k, v in hidden.items() if v)
            lines.append(f"{prefix}└── … {more} (raise limit_per_node)")

    walk(center, "")
    filtered = data.get("filtered") or {}
    notes = []
    if filtered.get("framework"):
        notes.append(f"{filtered['framework']} framework type(s) left out (include_framework=true)")
    if filtered.get("tests"):
        notes.append(f"{filtered['tests']} test entit(ies) left out (include_tests=true)")
    if len(lines) == 1:
        notes.append("no dependency in this direction")
    return "\n".join(lines + [f"({n})" for n in notes])


def _external_callers(session, repos: _Repos, name: str, limit: int) -> str:
    """For an external type: what the repository calls on it, and from where."""
    rows = _rows(
        session,
        "MATCH (f)-[r:CALLS]->(e:ExternalClass {name: $name}) RETURN labels(f)[0] AS kind, f.name AS fname, "
        "f.path AS path, f.line_number AS line, r.called_name AS member, r.line_number AS call_line",
        name=name,
    )
    info = _external_info(session, name)
    head = f"{EXTERNAL_PREFIX}{name}  [ExternalClass · {_origin_text(info)}]"
    if not rows:
        return head + "\n(no call into it)"
    callers: Dict[Tuple, Dict[str, Any]] = {}
    for r in rows:
        entry = callers.setdefault((r["path"], r["fname"], r["line"]), {**r, "members": set()})
        if r.get("member"):
            entry["members"].add(r["member"])
    nodes = [{"kind": c["kind"], "name": c["fname"], "indexed_path": c["path"], "line": c["line"]}
             for c in callers.values()]
    classes = _class_contexts(session, nodes)
    out = [head]
    ordered = sorted(callers.values(), key=lambda c: (str(c["path"]), c["line"] or 0))
    for i, c in enumerate(ordered[:limit]):
        last = i == min(len(ordered), limit) - 1 and len(ordered) <= limit
        cls = classes.get(nb._key(c["path"], c["fname"], c["line"]))
        ident = entity_id(repos, c["kind"], c["path"], c["fname"], c["line"], cls)
        out.append(f"{'└── ' if last else '├── '}called by ← {ident}  [{c['kind']}]  "
                   f"(.{', .'.join(sorted(c['members']))})")
    if len(ordered) > limit:
        out.append(f"└── … {len(ordered) - limit} more callers (raise limit_per_node)")
    return "\n".join(out)


def _external_info(session, name: str) -> Dict[str, Any]:
    try:
        rows = _rows(session, "MATCH (e:ExternalClass {name: $name}) RETURN e.origin AS origin, "
                              "e.package AS package, e.package_version AS package_version, "
                              "e.evidence AS evidence, e.candidates AS candidates", name=name)
    except Exception:  # noqa: BLE001 - database indexed before origins existed
        rows = []
    return rows[0] if rows else {}


# ---------------------------------------------------------------------------
# read_code
# ---------------------------------------------------------------------------


def read_code(session, ids: Sequence[str], mapper: Optional[PathMapper] = None) -> str:
    """Full code of entities, with their location; a file gives its outline."""
    mapper = mapper or PathMapper()
    if isinstance(ids, str):
        ids = [ids]
    if not ids:
        raise AgentToolError("Give at least one entity id.")
    repos = _Repos(session)
    blocks = []
    for ref in ids[:10]:
        try:
            target = resolve(session, repos, ref)
        except AgentToolError as exc:
            blocks.append(f"{ref}: {exc}")
            continue
        if target["kind"] == "ExternalClass":
            info = _external_info(session, target["name"])
            members = _rows(session, "MATCH ()-[r:CALLS]->(e:ExternalClass {name: $name}) "
                                     "RETURN DISTINCT r.called_name AS m", name=target["name"])
            blocks.append(
                f"{EXTERNAL_PREFIX}{target['name']}  [ExternalClass]\n"
                f"Not in the indexed sources: {_origin_text(info)}.\n"
                "Members the code calls: " + (", ".join(sorted(r["m"] for r in members if r.get("m"))) or "none")
                + "\n(traverse_graph on this id lists the callers; add the package's repository as a "
                "companion source to index its code)")
            continue
        local = mapper.to_local(target["path"]) or target["path"]
        if target["kind"] == "File":
            blocks.append(_outline(session, repos, target["path"]))
            continue
        line, end = _to_int(target.get("line")), _to_int(target.get("end_line"))
        if end is None:
            rows = _rows(session, f"MATCH (n:{target['kind']}) WHERE n.path = $path AND n.name = $name "
                                  "AND n.line_number = $line RETURN n.end_line AS e",
                         path=target["path"], name=target["name"], line=line)
            end = _to_int(rows[0]["e"]) if rows else line
        node = {"kind": target["kind"], "name": target["name"], "indexed_path": target["path"], "line": line}
        cls = _class_contexts(session, [node]).get(nb._key(target["path"], target["name"], line))
        text = _source_lines(local, line, end, MAX_READ_LINES)
        cut = end and line and end - line + 1 > MAX_READ_LINES
        numbered = "\n".join(f"{line + i:>5}  {t}" for i, t in enumerate(text))
        blocks.append(
            f"{entity_id(repos, target['kind'], target['path'], target['name'], line, cls)}  [{target['kind']}]\n"
            f"File: {local}  lines {line}-{end}\n{numbered or '(source not readable on this machine)'}"
            + (f"\n… truncated at {MAX_READ_LINES} lines" if cut else ""))
    return "\n\n".join(blocks)


def _outline(session, repos: _Repos, path: str) -> str:
    rows = _rows(session, nb._contains_query(), path=path)
    rows = [r for r in rows if r.get("b_name") != MODULE_PSEUDO_FUNCTION]
    rows.sort(key=lambda r: _to_int(r.get("b_line")) or 0)
    nodes = [{"kind": r["b_kind"], "name": r["b_name"], "indexed_path": r["b_path"], "line": r["b_line"]}
             for r in rows]
    classes = _class_contexts(session, nodes)
    out = [f"{repos.rel(path)}  [File, {len(rows)} entities]"]
    for r in rows:
        cls = classes.get(nb._key(r["b_path"], r["b_name"], r["b_line"]))
        out.append(f"  {r['b_line']:>5}-{r.get('b_end_line') or r['b_line']:<5} "
                   f"{entity_id(repos, r['b_kind'], r['b_path'], r['b_name'], r['b_line'], cls)}  [{r['b_kind']}]")
    out.append("(read_code on an entity id gives its code)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# graph_overview
# ---------------------------------------------------------------------------


def graph_overview(session, mapper: Optional[PathMapper] = None) -> str:
    """What is indexed and where to start reading."""
    mapper = mapper or PathMapper()
    repos = _Repos(session)
    if not repos.rows:
        return "Nothing is indexed yet: run `cviz index` (or `cviz graph --watch .`) in the repository."
    out = ["Repositories:"]
    for repo in sorted(repos.rows, key=lambda r: (r["companion"], r["name"])):
        files = _rows(session, "MATCH (f:File) WHERE f.path STARTS WITH $p RETURN count(f) AS n",
                      p=repo["path"] + "/")
        n = files[0]["n"] if files else 0
        role = "companion source" if repo["companion"] else "main"
        out.append(f"  {repo['name']}/  ({role}, {n} files)  {mapper.to_local(repo['path'])}")

    counts = []
    for label in ("Function", "Class", "Interface", "Record", "Struct", "Enum", "ExternalClass"):
        try:
            rows = _rows(session, f"MATCH (n:{label}) RETURN count(n) AS n")
        except Exception:  # noqa: BLE001 - label absent
            continue
        if rows and rows[0]["n"]:
            counts.append(f"{rows[0]['n']} {label}")
    out.append("Entities: " + ", ".join(counts))

    try:
        packages = _rows(session, "MATCH ()-[r:CALLS]->(e:ExternalClass) WHERE e.origin = 'package' "
                                  "RETURN e.package AS p, count(r) AS n ORDER BY n DESC LIMIT 8")
    except Exception:  # noqa: BLE001
        packages = []
    if packages:
        out.append("Most called packages: " + ", ".join(f"{p['p']} ({p['n']} calls)" for p in packages))

    main = repos.main
    entries = nb.entry_points(session, main["path"] + "/", limit=8, mapper=mapper)
    if entries:
        nodes = [{"kind": "Function", "name": e["name"], "indexed_path": e["indexed_path"], "line": e["line"]}
                 for e in entries]
        classes = _class_contexts(session, nodes)
        out.append("Entry points (nothing calls them, they call the most):")
        for e in entries:
            cls = classes.get(nb._key(e["indexed_path"], e["name"], e["line"]))
            out.append(f"  {entity_id(repos, 'Function', e['indexed_path'], e['name'], e['line'], cls)}")
    out.append("Ids: <path in the main repository>[:<Class.member>[@line]]; companion files start with "
               "the companion's name; external:<Type> for package types. "
               "search_code finds entities, traverse_graph walks their dependencies, read_code shows code.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Dispatch (MCP server on its own database, or the graph server for it)
# ---------------------------------------------------------------------------

TOOL_NAMES = ("graph_overview", "search_code", "traverse_graph", "read_code", "execute_cypher_query")


def call_tool(db_manager, name: str, args: Dict[str, Any], mapper: Optional[PathMapper] = None,
              engine=None) -> Dict[str, Any]:
    """``{"text": ...}`` for the agent, or ``{"error": ...}``."""
    args = dict(args or {})
    if name == "execute_cypher_query":
        from .tools.handlers.query_handlers import execute_cypher_query

        return execute_cypher_query(db_manager, **args)
    if name not in TOOL_NAMES:
        return {"error": f"Unknown tool: {name}"}
    mapper = mapper or PathMapper.from_env()
    try:
        with db_manager.get_driver().session() as session:
            if name == "graph_overview":
                text = graph_overview(session, mapper)
            elif name == "search_code":
                text = search_code(session, str(args.get("query") or ""), limit=args.get("limit") or 10,
                                   detail=args.get("detail") or "preview", mapper=mapper, engine=engine)
            elif name == "traverse_graph":
                text = traverse_graph(
                    session, args.get("start") or [], direction=args.get("direction") or "downstream",
                    depth=args.get("depth") or 1, relations=args.get("relations") or ("CALLS", "INHERITS"),
                    entity_types=args.get("entity_types"),
                    include_framework=bool(args.get("include_framework", False)),
                    include_tests=bool(args.get("include_tests", True)),
                    limit_per_node=args.get("limit_per_node") or 25, mapper=mapper)
            else:
                text = read_code(session, args.get("ids") or [], mapper=mapper)
    except AgentToolError as exc:
        return {"error": str(exc)}
    return {"text": text}
