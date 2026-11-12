"""Locate an artifact from ``file:line`` and walk its bounded neighbourhood.

Backs the standalone dependency-graph page (``/graph``) and its two read
endpoints, ``GET /api/locate`` and ``GET /api/neighborhood``.

Design notes
------------
* **Identity.** A code artifact is identified by ``(path, name, line)`` —
  never by its name alone, so two homonymous functions in different files (or
  two overloads in the same file) stay distinct. A file is identified by its
  path with ``kind == "File"``.
* **Portability.** Queries stick to the Cypher subset the backends share
  (LadybugDB, through the embedded translator):
  ``labels(x)[0]``, ``type(r)``, ``IN $list`` and ``CALLS|HEURISTIC_CALLS``
  alternation.
* **Bounded walk.** The neighbourhood is a breadth-first walk in which callers
  only expand towards their own callers and callees towards their own callees
  (likewise for parents/children in the inheritance hierarchy), so depth 2 does
  not explode into every sibling of every neighbour. Each node shows at most
  ``per_node_limit`` neighbours per direction and the whole response at most
  ``max_nodes`` nodes; anything cut is reported as a ``hidden`` count on the
  node it hangs from so the page can display "+N".
* **Path mapping.** When the index was built on another machine, the paths it
  stores differ from the developer's checkout. ``PathMapper`` rewrites
  ``indexed prefix -> local prefix`` on the way out and the reverse on the way
  in (``CVIZ_PATH_MAP="/srv/build/repo=/Users/me/repo;..."``).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Labels that carry ``path``/``line_number``/``end_line`` and can be the target
# of a navigation. Ordered from most to least specific for tie-breaking in
# ``locate``: a method wins over the class that contains it at equal span.
ARTIFACT_LABELS: Tuple[str, ...] = (
    "Function",
    "Class",
    "Interface",
    "Struct",
    "Enum",
    "Trait",
    "Record",
)

MODULE_PSEUDO_FUNCTION = "<module>"

CALL_RELS = "CALLS|HEURISTIC_CALLS"
INHERIT_RELS = "INHERITS|IMPLEMENTS"

SUPPORTED_KINDS = ("CALLS", "INHERITS", "IMPORTS")

# Test code, by path: a `tests/` or `*.Tests/` folder, or a test file name.
_TEST_PATH = re.compile(
    r"(^|/)(tests?|specs?|__tests__|testing)(/|$)"
    r"|(^|/)[^/]*[.](unit|integration|functional)?tests?(/|$)"
    r"|(^|/)(test_[^/]*|[^/]*_test[.][a-z]+|[^/]*[.](test|spec)[.][jt]sx?)$",
    re.IGNORECASE,
)
# `FooTests.cs`, not `Contest.cs`: the C# convention is case-sensitive.
_TEST_CS_FILE = re.compile(r"Tests?[.]cs$")
# Packages only test code uses: their types go with the test code.
_TEST_PACKAGES = re.compile(
    r"^(moq|xunit|nunit|mstest|autofixture|fluentassertions|nsubstitute|fakeiteasy|"
    r"shouldly|bogus|coverlet|microsoft[.]net[.]test|microsoft[.]visualstudio[.]testplatform)([.]|$)",
    re.IGNORECASE,
)


def is_test_path(path: Optional[str]) -> bool:
    norm = _norm(path or "")
    return bool(norm) and bool(_TEST_PATH.search(norm) or _TEST_CS_FILE.search(norm))


DEFAULT_MAX_NODES = 300
DEFAULT_PER_NODE_LIMIT = 60
MAX_DEPTH = 5


class ArtifactNotFound(LookupError):
    """Raised when nothing in the graph matches the requested artifact."""


# ---------------------------------------------------------------------------
# Path mapping
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    return path.replace("\\", "/")


@dataclass
class PathMapper:
    """Bidirectional ``indexed prefix <-> local prefix`` rewriting."""

    pairs: List[Tuple[str, str]] = field(default_factory=list)

    @classmethod
    def parse(cls, spec: Optional[str]) -> "PathMapper":
        """Parse ``"indexed=local;indexed2=local2"`` (``\\n`` also separates)."""
        pairs: List[Tuple[str, str]] = []
        if spec:
            for chunk in spec.replace("\n", ";").split(";"):
                chunk = chunk.strip()
                if not chunk or "=" not in chunk:
                    continue
                indexed, local = chunk.split("=", 1)
                indexed, local = _norm(indexed.strip()), _norm(local.strip())
                if indexed and local:
                    pairs.append((indexed.rstrip("/"), local.rstrip("/")))
        # Longest prefix first so nested mappings win over their parents.
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        return cls(pairs)

    @classmethod
    def from_env(cls) -> "PathMapper":
        return cls.parse(os.getenv("CVIZ_PATH_MAP"))

    @staticmethod
    def _swap(path: str, src: str, dst: str) -> Optional[str]:
        if path == src:
            return dst
        if path.startswith(src + "/"):
            return dst + path[len(src):]
        return None

    def to_local(self, path: Optional[str]) -> Optional[str]:
        if not path or not self.pairs:
            return path
        p = _norm(path)
        for indexed, local in self.pairs:
            swapped = self._swap(p, indexed, local)
            if swapped is not None:
                return swapped
        return path

    def to_indexed(self, path: Optional[str]) -> Optional[str]:
        if not path or not self.pairs:
            return path
        p = _norm(path)
        for indexed, local in sorted(self.pairs, key=lambda x: len(x[1]), reverse=True):
            swapped = self._swap(p, local, indexed)
            if swapped is not None:
                return swapped
        return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def node_id(kind: str, path: Optional[str], name: Optional[str], line: Optional[int]) -> str:
    """Stable client-side identifier: ``kind|path|name|line``."""
    if kind == "File":
        return f"File|{path or ''}"
    if not path:
        # External symbols (ExternalClass, Module) have no location.
        return f"{kind}|{name or ''}"
    return f"{kind}|{path}|{name or ''}|{line if line is not None else ''}"


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _display_name(kind: str, name: Optional[str], path: Optional[str]) -> str:
    if kind == "File" or name == MODULE_PSEUDO_FUNCTION:
        base = PurePosixPath(_norm(path or "")).name or (name or "?")
        return base if kind == "File" else f"{base} (module)"
    return name or "?"


def _rows(session, query: str, **params) -> List[Dict[str, Any]]:
    result = session.run(query, **params)
    if hasattr(result, "data"):
        return list(result.data())
    return [dict(r) for r in result]


def _key(path: Optional[str], name: Optional[str], line: Optional[int]) -> Tuple[str, str, Optional[int]]:
    return (_norm(path or ""), name or "", _to_int(line))


# ---------------------------------------------------------------------------
# Locate
# ---------------------------------------------------------------------------


def _artifact_from_row(row: Dict[str, Any], mapper: PathMapper) -> Dict[str, Any]:
    kind = row.get("kind") or "Function"
    path = row.get("path")
    name = row.get("name")
    line = _to_int(row.get("line"))
    return {
        "id": node_id(kind, path, name, line),
        "kind": kind,
        "name": name,
        "label": _display_name(kind, name, path),
        "path": mapper.to_local(path),
        "indexed_path": path,
        "line": line,
        "end_line": _to_int(row.get("end_line")),
        "complexity": _to_int(row.get("complexity")),
    }


def locate(session, path: str, line: Optional[int], mapper: Optional[PathMapper] = None) -> Dict[str, Any]:
    """Return the narrowest artifact enclosing ``path:line``.

    Preference order: the smallest ``end_line - line_number`` span, then the
    label order of :data:`ARTIFACT_LABELS` (function before class). The
    ``<module>`` pseudo-function, which spans the whole file, is only used when
    nothing narrower matches; with no enclosing artifact at all the file itself
    is returned. Raises :class:`ArtifactNotFound` when the file is not indexed.
    """
    mapper = mapper or PathMapper()
    indexed_path = _norm(mapper.to_indexed(path) or path)

    candidates: List[Tuple[Tuple[int, int, int], Dict[str, Any]]] = []
    if line is not None:
        for rank, label in enumerate(ARTIFACT_LABELS):
            query = (
                f"MATCH (n:{label}) WHERE n.path = $path "
                "AND n.line_number <= $line AND n.end_line >= $line "
                "RETURN n.name AS name, n.path AS path, n.line_number AS line, "
                "n.end_line AS end_line, n.cyclomatic_complexity AS complexity"
            )
            if label != "Function":
                # Only Function nodes carry cyclomatic_complexity on every
                # backend; KùzuDB rejects unknown properties on typed tables.
                query = query.replace(", n.cyclomatic_complexity AS complexity", "")
            try:
                rows = _rows(session, query, path=indexed_path, line=int(line))
            except Exception:  # noqa: BLE001 - a label missing on one backend is not fatal
                continue
            for row in rows:
                row["kind"] = label
                start = _to_int(row.get("line")) or 0
                end = _to_int(row.get("end_line")) or start
                is_module = 1 if row.get("name") == MODULE_PSEUDO_FUNCTION else 0
                candidates.append(((is_module, end - start, rank), row))

    if candidates:
        candidates.sort(key=lambda c: c[0])
        return _artifact_from_row(candidates[0][1], mapper)

    file_rows = _rows(
        session,
        "MATCH (f:File) WHERE f.path = $path RETURN f.path AS path, f.name AS name",
        path=indexed_path,
    )
    if file_rows:
        row = dict(file_rows[0])
        row["kind"] = "File"
        row["line"] = 1
        return _artifact_from_row(row, mapper)

    raise ArtifactNotFound(
        f"'{path}' is not in the graph. Index its repository first "
        f"(cviz index <repo>) or check the path mapping (CVIZ_PATH_MAP)."
    )


def resolve_symbol(session, symbol: str, file: Optional[str] = None,
                   mapper: Optional[PathMapper] = None) -> List[Dict[str, Any]]:
    """Find artifacts named ``symbol`` (optionally restricted to ``file``).

    Used for links of the form ``/graph?symbol=foo&file=bar.py``. Returns every
    match so the page can let the user disambiguate homonyms.
    """
    mapper = mapper or PathMapper()
    found: List[Dict[str, Any]] = []
    indexed_file = _norm(mapper.to_indexed(file) or file) if file else None
    for label in ARTIFACT_LABELS:
        where = "n.name = $name" + (" AND n.path = $path" if indexed_file else "")
        query = (
            f"MATCH (n:{label}) WHERE {where} "
            "RETURN n.name AS name, n.path AS path, n.line_number AS line, n.end_line AS end_line "
            "LIMIT 50"
        )
        params: Dict[str, Any] = {"name": symbol}
        if indexed_file:
            params["path"] = indexed_file
        try:
            rows = _rows(session, query, **params)
        except Exception:  # noqa: BLE001
            continue
        for row in rows:
            row["kind"] = label
            found.append(_artifact_from_row(row, mapper))
    found.sort(key=lambda a: (a["path"] or "", a["line"] or 0))
    return found


# Entry points: where to start reading a repository.

ENTRY_POINT_NAMES = frozenset({
    "main", "__main__", "run", "start", "serve", "app", "cli", "handler", "lambda_handler",
    "index", "setup", "bootstrap", "init", "program", "execute",
})
ENTRY_POINT_FILES = frozenset({"main", "__main__", "app", "cli", "server", "manage", "index", "program", "run"})
# Vendored, generated, minified and test code is never a reading entry point.
_NOT_ENTRY_PATH = re.compile(
    r"(\.min\.(js|css)$|[\\/](vendor|node_modules|dist|build|third_party|__pycache__|\.venv|venv)[\\/]"
    r"|[\\/]tests?[\\/]|[\\/]__tests__[\\/]|(^|[\\/])test_[^\\/]*$|_test\.\w+$|\.(spec|test)\.\w+$"
    r"|Tests?\.\w+$)",
    re.IGNORECASE,
)
_ENTRY_CANDIDATES = 300
_ENTRY_PER_FILE = 2


def entry_points(session, prefix: Optional[str], limit: int = 6,
                 mapper: Optional[PathMapper] = None) -> List[Dict[str, Any]]:
    """Functions to start reading a repository from, best first.

    A code graph has no single root. An entry point is a function nothing
    calls (no CALLS / HEURISTIC_CALLS edge reaches it) that calls others:
    ranked by how many distinct artifacts it calls, conventional names
    (``main``, ``run``, a ``main.py`` / ``app.py`` module…) first. Vendored,
    generated, minified and test code is skipped, and a file contributes at
    most two entries so one big file cannot fill the list.
    """
    mapper = mapper or PathMapper()
    scope = "n.path STARTS WITH $prefix AND " if prefix else ""
    query = (
        f"MATCH (n:Function) WHERE {scope}(n.is_dependency IS NULL OR NOT n.is_dependency) "
        "AND NOT EXISTS { MATCH ()-[:CALLS|HEURISTIC_CALLS]->(n) } "
        "MATCH (n)-[:CALLS|HEURISTIC_CALLS]->(m) "
        "RETURN n.name AS name, n.path AS path, n.line_number AS line, n.end_line AS end_line, "
        f"count(DISTINCT m) AS out ORDER BY out DESC LIMIT {_ENTRY_CANDIDATES}"
    )
    params = {"prefix": prefix} if prefix else {}
    runner = getattr(session, "run_native", None)
    rows = runner(query, **params).data() if runner else _rows(session, query, **params)

    def score(row: Dict[str, Any]) -> float:
        name = str(row.get("name") or "")
        stem = PurePosixPath(str(row.get("path") or "").replace("\\", "/")).stem.lower()
        bonus = 0.0
        if name.lower() in ENTRY_POINT_NAMES:
            bonus += 1000
        if name == MODULE_PSEUDO_FUNCTION and stem in ENTRY_POINT_FILES:
            bonus += 1000
        return bonus + float(row.get("out") or 0)

    picked: List[Dict[str, Any]] = []
    per_file: Dict[str, int] = {}
    for row in sorted(rows, key=score, reverse=True):
        path = str(row.get("path") or "")
        if not path or _NOT_ENTRY_PATH.search(path):
            continue
        if per_file.get(path, 0) >= _ENTRY_PER_FILE:
            continue
        per_file[path] = per_file.get(path, 0) + 1
        artifact = _artifact_from_row(dict(row, kind="Function"), mapper)
        artifact["calls"] = int(row.get("out") or 0)
        picked.append(artifact)
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------------------
# Neighbourhood
# ---------------------------------------------------------------------------


@dataclass
class _Frontier:
    """Nodes to expand at the current BFS level, grouped by expansion role."""

    # role -> list of (kind, path, name, line, node_id)
    items: Dict[str, List[Tuple[str, str, str, Optional[int], str]]] = field(default_factory=dict)

    def add(self, role: str, kind: str, path: str, name: str, line: Optional[int], nid: str) -> None:
        self.items.setdefault(role, []).append((kind, path, name, line, nid))

    def __bool__(self) -> bool:
        return any(self.items.values())


# (relationship alternation, direction) for each expansion role. "out" means
# the frontier node is the relationship's source.
_ROLE_QUERIES: Dict[str, Tuple[str, str, str]] = {
    # role: (kind family, rel alternation, direction)
    "callee": ("CALLS", CALL_RELS, "out"),
    "caller": ("CALLS", CALL_RELS, "in"),
    "parent": ("INHERITS", INHERIT_RELS, "out"),
    "child": ("INHERITS", INHERIT_RELS, "in"),
    "import": ("IMPORTS", "IMPORTS", "out"),
}

# Which roles a node of a given role keeps expanding into at the next level.
_NEXT_ROLES: Dict[str, Tuple[str, ...]] = {
    "callee": ("callee",),
    "caller": ("caller",),
    "parent": ("parent",),
    "child": ("child",),
    "import": (),
    "member": (),
}


def _expand_query(rel_alternation: str, direction: str, frontier_is_file: bool,
                  with_call_line: bool = True) -> str:
    """One query per (role, level): every frontier node is matched at once."""
    if direction == "out":
        pattern = f"(a)-[r:{rel_alternation}]->(b)"
    else:
        pattern = f"(b)-[r:{rel_alternation}]->(a)"
    if frontier_is_file:
        where = "a.path IN $paths AND labels(a)[0] = 'File'"
    else:
        where = "a.path IN $paths AND a.name IN $names"
    return (
        f"MATCH {pattern} WHERE {where} "
        "RETURN labels(a)[0] AS a_kind, a.path AS a_path, a.name AS a_name, a.line_number AS a_line, "
        "labels(b)[0] AS b_kind, b.path AS b_path, b.name AS b_name, b.line_number AS b_line, "
        "b.end_line AS b_end_line, type(r) AS rel"
        # Only CALLS edges carry the call-site line and the called member;
        # KùzuDB/LadybugDB reject the properties on INHERITS/IMPORTS tables
        # that do not declare them.
        + (", r.line_number AS call_line, r.called_name AS called_name" if with_call_line else "")
    )


def _contains_query() -> str:
    return (
        "MATCH (f:File)-[:CONTAINS]->(b) WHERE f.path = $path "
        "AND (b:Function OR b:Class OR b:Interface OR b:Struct OR b:Enum OR b:Trait OR b:Record) "
        "RETURN labels(b)[0] AS b_kind, b.path AS b_path, b.name AS b_name, "
        "b.line_number AS b_line, b.end_line AS b_end_line"
    )


def _complexities(session, keys: Iterable[Tuple[str, str, Optional[int]]]) -> Dict[Tuple[str, str, Optional[int]], int]:
    keys = [k for k in keys if k[0]]
    if not keys:
        return {}
    paths = sorted({k[0] for k in keys})
    names = sorted({k[1] for k in keys})
    try:
        rows = _rows(
            session,
            "MATCH (n:Function) WHERE n.path IN $paths AND n.name IN $names "
            "RETURN n.path AS path, n.name AS name, n.line_number AS line, "
            "n.cyclomatic_complexity AS complexity",
            paths=paths,
            names=names,
        )
    except Exception:  # noqa: BLE001 - complexity is decorative
        return {}
    wanted = set(keys)
    out: Dict[Tuple[str, str, Optional[int]], int] = {}
    for row in rows:
        k = _key(row.get("path"), row.get("name"), row.get("line"))
        c = _to_int(row.get("complexity"))
        if k in wanted and c is not None:
            out[k] = c
    return out


_ORIGIN_FIELDS = ("origin", "package", "package_version", "evidence", "candidates")


def _external_origins(session, names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Package or framework of ExternalClass nodes, by name (step of indexing
    that may not have run: then nothing is known)."""
    names = sorted({n for n in names if n})
    if not names:
        return {}
    try:
        rows = _rows(
            session,
            "MATCH (e:ExternalClass) WHERE e.name IN $names RETURN e.name AS name, "
            + ", ".join(f"e.{f} AS {f}" for f in _ORIGIN_FIELDS),
            names=names,
        )
    except Exception:  # noqa: BLE001 - database indexed before origins existed
        return {}
    return {r["name"]: {f: r.get(f) for f in _ORIGIN_FIELDS} for r in rows if r.get("name")}


def neighborhood(
    session,
    *,
    path: str,
    name: Optional[str] = None,
    line: Optional[int] = None,
    kind: Optional[str] = None,
    depth: int = 1,
    kinds: Sequence[str] = ("CALLS", "INHERITS"),
    max_nodes: int = DEFAULT_MAX_NODES,
    per_node_limit: int = DEFAULT_PER_NODE_LIMIT,
    mapper: Optional[PathMapper] = None,
    hide_framework: bool = False,
    hide_tests: bool = False,
    roles: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Return ``{"center", "nodes", "links", "truncated"}`` around an artifact.

    The center is identified by ``path`` + ``name`` + ``line`` (or ``kind ==
    "File"`` for a file). When ``name`` is omitted, ``line`` is treated as a
    cursor position and resolved with :func:`locate` first.

    *hide_framework* leaves out external types of the language's base library
    (``String``, ``Task``, ``ILogger``…); *hide_tests* leaves out test code and
    the types of test-only packages (Moq, xunit…), unless the center itself is
    test code. Both apply before the per-node cap, so what is left out does not
    take the room of what is shown; the counts are in ``filtered``.

    *roles* restricts the walk to some directions: ``callee`` / ``caller``
    (CALLS), ``parent`` / ``child`` (INHERITS), ``import`` (IMPORTS of a
    file); all of those allowed by *kinds* when omitted.
    """
    mapper = mapper or PathMapper()
    depth = max(1, min(MAX_DEPTH, int(depth or 1)))
    kinds = tuple(k.upper() for k in kinds if k and k.upper() in SUPPORTED_KINDS) or ("CALLS",)
    max_nodes = max(1, int(max_nodes))
    per_node_limit = max(1, int(per_node_limit))

    if kind == "File":
        center = locate(session, path, None, mapper)  # file fallback branch
        if center["kind"] != "File":
            center = {**center, "kind": "File"}
    elif name is None:
        center = locate(session, path, line, mapper)
    else:
        indexed = _norm(mapper.to_indexed(path) or path)
        center_kind = kind or "Function"
        center = _artifact_from_row(
            {"kind": center_kind, "path": indexed, "name": name, "line": line}, mapper
        )
        # Confirm the artifact exists (and pick up end_line / exact kind).
        matches = [
            a for a in resolve_symbol(session, name, path, mapper)
            if a["line"] == _to_int(line) or line is None
        ]
        if not matches:
            raise ArtifactNotFound(f"No artifact '{name}' at {path}:{line} in the graph.")
        center = matches[0]

    nodes: Dict[str, Dict[str, Any]] = {}
    links: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    truncated = False

    def add_node(kind_: str, ipath: Optional[str], name_: Optional[str], line_: Optional[int],
                 end_line: Optional[int], role: str, level: int) -> Tuple[str, bool]:
        nid = node_id(kind_, ipath, name_, line_)
        if nid in nodes:
            return nid, False
        nodes[nid] = {
            "id": nid,
            "kind": kind_,
            "name": name_,
            "label": _display_name(kind_, name_, ipath),
            "path": mapper.to_local(ipath),
            "indexed_path": ipath,
            "line": line_,
            "end_line": end_line,
            "complexity": None,
            "role": role,
            "depth": level,
            "external": not ipath,
            "hidden": {},
        }
        if kind_ == "ExternalClass":
            nodes[nid].update(origins.get(name_ or "") or {f: None for f in _ORIGIN_FIELDS})
        return nid, True

    origins: Dict[str, Dict[str, Any]] = {}
    filtered = {"framework": 0, "tests": 0}

    def left_out(row: Dict[str, Any]) -> Optional[str]:
        """Which filter, if any, leaves the row's target out."""
        if row.get("b_kind") == "ExternalClass":
            info = origins.get(row.get("b_name") or "") or {}
            if hide_framework and info.get("origin") == "framework":
                return "framework"
            if hide_tests and _TEST_PACKAGES.match(info.get("package") or ""):
                return "tests"
        elif hide_tests and is_test_path(row.get("b_path")):
            return "tests"
        return None

    center_ipath = center.get("indexed_path") or center.get("path")
    cid, _ = add_node(center["kind"], center_ipath, center.get("name"), center.get("line"),
                      center.get("end_line"), "center", 0)
    nodes[cid]["complexity"] = center.get("complexity")
    if hide_tests and is_test_path(center_ipath):
        hide_tests = False  # around a test, the tests are the point

    frontier = _Frontier()
    is_file_center = center["kind"] == "File"
    start_roles: List[str] = []
    if "CALLS" in kinds and not is_file_center:
        start_roles += ["caller", "callee"]
    if "INHERITS" in kinds and not is_file_center:
        start_roles += ["parent", "child"]
    if "IMPORTS" in kinds and is_file_center:
        start_roles.append("import")
    if roles is not None:
        start_roles = [r for r in start_roles if r in roles]
    for role in start_roles:
        frontier.add(role, center["kind"], _norm(center_ipath or ""), center.get("name") or "",
                     _to_int(center.get("line")), cid)

    # A file center also lists the artifacts it declares, so the page always
    # has something to recenter on.
    if is_file_center:
        try:
            member_rows = _rows(session, _contains_query(), path=_norm(center_ipath or ""))
        except Exception:  # noqa: BLE001
            member_rows = []
        member_rows.sort(key=lambda r: _to_int(r.get("b_line")) or 0)
        shown = 0
        for row in member_rows:
            if row.get("b_name") == MODULE_PSEUDO_FUNCTION:
                continue
            if shown >= per_node_limit or len(nodes) >= max_nodes:
                nodes[cid]["hidden"]["member"] = nodes[cid]["hidden"].get("member", 0) + 1
                truncated = True
                continue
            mid, _ = add_node(row.get("b_kind") or "Function", row.get("b_path"), row.get("b_name"),
                              _to_int(row.get("b_line")), _to_int(row.get("b_end_line")), "member", 1)
            links[(cid, mid, "CONTAINS")] = {"source": cid, "target": mid, "type": "CONTAINS", "line": None}
            shown += 1

    for level in range(1, depth + 1):
        if not frontier:
            break
        next_frontier = _Frontier()
        for role, items in frontier.items.items():
            family, rels, direction = _ROLE_QUERIES[role]
            by_file = [i for i in items if i[0] == "File"]
            by_symbol = [i for i in items if i[0] != "File"]
            for group, file_mode in ((by_symbol, False), (by_file, True)):
                if not group:
                    continue
                wanted = {_key(p, n, l): nid for (_k, p, n, l, nid) in group} if not file_mode \
                    else {(_norm(p), "", None): nid for (_k, p, n, l, nid) in group}
                params: Dict[str, Any] = {"paths": sorted({_norm(i[1]) for i in group})}
                if not file_mode:
                    params["names"] = sorted({i[2] for i in group})
                with_line = family == "CALLS"
                try:
                    rows = _rows(session, _expand_query(rels, direction, file_mode, with_line), **params)
                except Exception:  # noqa: BLE001 - e.g. IMPLEMENTS table missing on a backend
                    rows = []
                    for single in rels.split("|") if "|" in rels else ():
                        try:
                            rows += _rows(session, _expand_query(single, direction, file_mode, with_line), **params)
                        except Exception:  # noqa: BLE001
                            continue

                if family == "CALLS":
                    origins.update(_external_origins(session, (
                        r.get("b_name") for r in rows
                        if r.get("b_kind") == "ExternalClass" and r.get("b_name") not in origins
                    )))

                # Group rows by their frontier node, then cap per node.
                per_source: Dict[str, List[Dict[str, Any]]] = {}
                seen_left_out: set = set()
                for row in rows:
                    k = (_norm(row.get("a_path") or ""), "", None) if file_mode \
                        else _key(row.get("a_path"), row.get("a_name"), row.get("a_line"))
                    src_id = wanted.get(k)
                    if src_id is None:
                        continue
                    reason = left_out(row)
                    if reason:
                        target_key = (src_id, row.get("b_kind"), row.get("b_path"), row.get("b_name"), row.get("b_line"))
                        if target_key not in seen_left_out:
                            seen_left_out.add(target_key)
                            filtered[reason] += 1
                        continue
                    per_source.setdefault(src_id, []).append(row)

                for src_id, src_rows in per_source.items():
                    # Deduplicate targets (several call sites to the same
                    # callee become one edge listing every call line).
                    targets: Dict[str, Dict[str, Any]] = {}
                    for row in src_rows:
                        b_kind = row.get("b_kind") or "Function"
                        b_line = _to_int(row.get("b_line"))
                        tid = node_id(b_kind, row.get("b_path"), row.get("b_name"), b_line)
                        entry = targets.setdefault(
                            tid, {"row": row, "call_lines": set(), "members": set(), "heuristic": True})
                        cl = _to_int(row.get("call_line"))
                        if cl is not None:
                            entry["call_lines"].add(cl)
                        if row.get("called_name") and b_kind == "ExternalClass":
                            entry["members"].add(row["called_name"])
                        if row.get("rel") != "HEURISTIC_CALLS":
                            entry["heuristic"] = False
                    ordered = sorted(
                        targets.items(),
                        key=lambda kv: (min(kv[1]["call_lines"]) if kv[1]["call_lines"] else 1 << 30, kv[0]),
                    )
                    shown = 0
                    for tid, entry in ordered:
                        if tid == src_id:
                            # Recursion: keep a self-loop edge, nothing to expand.
                            links[(src_id, src_id, family)] = {
                                "source": src_id, "target": src_id, "type": family,
                                "line": min(entry["call_lines"]) if entry["call_lines"] else None,
                                "lines": sorted(entry["call_lines"]),
                                "heuristic": entry["heuristic"],
                            }
                            continue
                        is_new = tid not in nodes
                        if is_new and (shown >= per_node_limit or len(nodes) >= max_nodes):
                            hidden = nodes[src_id]["hidden"]
                            hidden[role] = hidden.get(role, 0) + 1
                            truncated = True
                            continue
                        row = entry["row"]
                        b_line = _to_int(row.get("b_line"))
                        tid, created = add_node(
                            row.get("b_kind") or "Function", row.get("b_path"), row.get("b_name"),
                            b_line, _to_int(row.get("b_end_line")), role, level,
                        )
                        shown += 1
                        source, target = (src_id, tid) if direction == "out" else (tid, src_id)
                        lines = sorted(entry["call_lines"])
                        links[(source, target, family)] = {
                            "source": source,
                            "target": target,
                            "type": family,
                            "rel": row.get("rel"),
                            # Line of the call site (in the caller's file); the
                            # node itself carries its definition line.
                            "line": lines[0] if lines else None,
                            "lines": lines,
                            "heuristic": entry["heuristic"] if family == "CALLS" else False,
                        }
                        if entry["members"]:
                            # Members called on an external type, e.g. LogInformation on ILogger.
                            links[(source, target, family)]["members"] = sorted(entry["members"])
                        if created and level < depth and row.get("b_path"):
                            for next_role in _NEXT_ROLES.get(role, ()):
                                next_frontier.add(
                                    next_role, row.get("b_kind") or "Function",
                                    _norm(row.get("b_path") or ""), row.get("b_name") or "", b_line, tid,
                                )
        frontier = next_frontier

    # Nodes at the depth boundary may still have unexplored neighbours; the
    # page offers "Expand" for those. Complexity is fetched in one pass.
    comp = _complexities(
        session,
        [
            _key(n["indexed_path"], n["name"], n["line"])
            for n in nodes.values()
            if n["kind"] == "Function" and n["complexity"] is None
        ],
    )
    for n in nodes.values():
        if n["complexity"] is None and n["kind"] == "Function":
            n["complexity"] = comp.get(_key(n["indexed_path"], n["name"], n["line"]))

    center_node = nodes[cid]
    return {
        "center": center_node,
        "depth": depth,
        "kinds": list(kinds),
        "nodes": list(nodes.values()),
        "links": list(links.values()),
        "truncated": truncated,
        "filtered": filtered,
        "limits": {"max_nodes": max_nodes, "per_node": per_node_limit},
    }
