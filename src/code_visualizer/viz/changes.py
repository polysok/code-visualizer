"""What the current state of a repository changes against a target branch.

Backs the "Changes" view of the graph page (``/api/changes*``): the
entities a branch adds or modifies (from ``git diff`` lines matched against
the entity spans of the index), and who is impacted (their callers, the types
deriving from a changed type).

* **Against the merge base, like a pull request.** The branch is compared
  with the point where it left the target (``git merge-base``), so commits
  landed on the target since then do not show up as changes of the branch.
* **The current state is the working tree,** uncommitted edits included: it
  is what the index (kept up to date by ``cviz graph --watch``) describes.
* **Lost entities come from the old version of the files** (``git show``,
  parsed with the same tree-sitter parsers): what is gone from a file is
  *moved* when it reappears elsewhere under its name, *renamed* when its code
  reappears under another name, *deleted* otherwise.
* **Impact is what the graph resolves.** Calls through an interface are
  followed to the interface method the changed method implements (its
  callers may reach other implementations); calls the indexer could not
  resolve (reflection, dependency injection by name...) are not seen.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import neighborhood as nb

GIT_TIMEOUT = 30
FETCH_TIMEOUT = 180
MAX_CHANGED = 150       # changed entities shown; the rest is counted
CALLERS_PER_NODE = 15   # impacted callers shown per changed entity
MAX_NODES = 500

_DEFAULT_TARGETS = ("origin/main", "origin/master", "origin/develop", "main", "master", "develop")
_CLASS_LABELS = ("Class", "Interface", "Struct", "Record", "Trait", "Enum")


class GitError(RuntimeError):
    """git is missing, the folder is not a repository, or a command failed."""


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git(repo: str, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args], cwd=repo, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed (or not on the PATH of the cviz server)") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} took more than {timeout} s") from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip().splitlines()
        raise GitError(message[-1] if message else f"git {args[0]} failed ({proc.returncode})")
    return proc.stdout


def _fetched_at(repo: str) -> Optional[float]:
    """When the remotes were last fetched (FETCH_HEAD), if ever."""
    times = []
    for flag in ("--git-dir", "--git-common-dir"):
        try:
            git_dir = Path(repo, _git(repo, "rev-parse", flag).strip())
            times.append((git_dir / "FETCH_HEAD").stat().st_mtime)
        except (GitError, OSError):
            continue
    return max(times) if times else None


def branches(repo: str) -> Dict[str, Any]:
    """Branches to compare with, the current one, and a default target."""
    _git(repo, "rev-parse", "--is-inside-work-tree")
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    refs = []
    out = _git(repo, "for-each-ref", "--sort=-committerdate",
               "--format=%(refname)%09%(refname:short)%09%(committerdate:unix)", "refs/heads", "refs/remotes")
    for line in out.splitlines():
        full, short, when = (line.split("\t") + ["", "", ""])[:3]
        if full.endswith("/HEAD"):
            continue
        refs.append({"name": short, "remote": full.startswith("refs/remotes/"),
                     "date": int(when) if when.isdigit() else None})
    names = {r["name"] for r in refs}
    default = None
    try:  # the remote's default branch: what pull requests usually target
        head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
        if head in names:
            default = head
    except GitError:
        pass
    if not default:
        default = next((n for n in _DEFAULT_TARGETS if n in names and n != current), None)
    return {"current": current, "branches": refs, "default": default, "fetched_at": _fetched_at(repo)}


def fetch(repo: str) -> None:
    _git(repo, "fetch", "--all", "--prune", "--quiet", timeout=FETCH_TIMEOUT)


@dataclass
class FileChange:
    path: str                       # relative to the repository, posix
    status: str                     # added | modified | deleted | renamed
    old_path: Optional[str] = None
    # New-side line ranges (inclusive); `pure` when nothing was removed there.
    ranges: List[Tuple[int, int, bool]] = field(default_factory=list)
    # New-side lines after which old lines were removed.
    removals: List[int] = field(default_factory=list)

    def touches(self, start: int, end: int) -> bool:
        if any(a <= end and b >= start for a, b, _ in self.ranges):
            return True
        # Lines removed between `line` and `line + 1`, both inside the entity.
        return any(start <= line < end for line in self.removals)

    def only_added(self, start: int, end: int) -> bool:
        """The whole span is new code (a new function in an existing file)."""
        if self.status == "added":
            return True
        covered = set()
        for a, b, pure in self.ranges:
            if pure:
                covered.update(range(max(a, start), min(b, end) + 1))
        return len(covered) == end - start + 1


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _unquote(path: str) -> str:
    if len(path) >= 2 and path[0] == path[-1] == '"':
        return path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode(
            "latin-1", "replace").decode("utf-8", "replace")
    return path


def changes(repo: str, target: str) -> Dict[str, Any]:
    """Files and lines changed by the working tree since it left *target*."""
    known = {b["name"] for b in branches(repo)["branches"]}
    if target not in known:  # also keeps option-like values away from git
        raise GitError(f"no branch named '{target}' (fetch the remotes if it is new)")
    base = _git(repo, "merge-base", "HEAD", target).strip()

    files: Dict[str, FileChange] = {}
    status_out = _git(repo, "diff", "--name-status", "-M", "--relative", "--no-ext-diff", base)
    for line in status_out.splitlines():
        parts = line.split("\t")
        code = parts[0][:1]
        if code == "R" and len(parts) == 3:
            files[parts[2]] = FileChange(parts[2], "renamed", old_path=parts[1])
        elif code in "AMDT" and len(parts) >= 2:
            files[parts[1]] = FileChange(
                parts[1], {"A": "added", "D": "deleted"}.get(code, "modified"))

    current: Optional[FileChange] = None
    diff_out = _git(repo, "diff", "--unified=0", "-M", "--relative", "--no-color", "--no-ext-diff", base)
    for line in diff_out.splitlines():
        if line.startswith("+++ "):
            name = _unquote(line[4:].strip())
            current = files.get(name[2:]) if name.startswith("b/") else None
        elif line.startswith("@@") and current is not None:
            m = _HUNK.match(line)
            if not m:
                continue
            old_count = int(m.group(1)) if m.group(1) is not None else 1
            start = int(m.group(2))
            count = int(m.group(3)) if m.group(3) is not None else 1
            if count:
                current.ranges.append((start, start + count - 1, old_count == 0))
            else:
                current.removals.append(start)

    for name in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        # .cvizignore: written by the indexer itself, not part of the work.
        if name and name not in files and name != ".cvizignore":
            files[name] = FileChange(name, "added")
    return {"base": base, "base_short": base[:9], "files": sorted(files.values(), key=lambda f: f.path)}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def _entities(session, indexed_paths: Sequence[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for label in nb.ARTIFACT_LABELS:
        extra = ", n.class_context AS cls" if label == "Function" else ""
        try:
            rows = nb._rows(
                session,
                f"MATCH (n:{label}) WHERE n.path IN $paths RETURN n.name AS name, n.path AS path, "
                f"n.line_number AS line, n.end_line AS end_line{extra}",
                paths=list(indexed_paths),
            )
        except Exception:  # noqa: BLE001 - a label missing on one backend
            continue
        for row in rows:
            if row.get("name") == nb.MODULE_PSEUDO_FUNCTION or not row.get("line"):
                continue
            out.append(dict(row, kind=label))
    return out


def _caller_rows(session, items: Sequence[Dict[str, Any]], rels: str) -> List[Dict[str, Any]]:
    """Who has a *rels* edge to one of *items* (rows of nb._expand_query, "in")."""
    if not items:
        return []
    params = {"paths": sorted({i["path"] for i in items}), "names": sorted({i["name"] for i in items})}
    with_line = "CALLS" in rels
    try:
        rows = nb._rows(session, nb._expand_query(rels, "in", False, with_line), **params)
    except Exception:  # noqa: BLE001 - e.g. IMPLEMENTS missing: one relation at a time
        rows = []
        for single in rels.split("|"):
            try:
                rows += nb._rows(session, nb._expand_query(single, "in", False, with_line), **params)
            except Exception:  # noqa: BLE001
                continue
    wanted = {nb._key(i["path"], i["name"], i["line"]) for i in items}
    return [r for r in rows if nb._key(r.get("a_path"), r.get("a_name"), r.get("a_line")) in wanted]


def _base_methods(session, changed_functions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Methods of base types / interfaces that changed methods override or
    implement: their callers may run the changed code."""
    by_class: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for f in changed_functions:
        if f.get("cls"):
            by_class.setdefault((f["path"], f["cls"]), []).append(f)
    if not by_class:
        return []
    bases: List[Dict[str, Any]] = []
    for rel in ("INHERITS", "IMPLEMENTS"):
        try:
            bases += nb._rows(
                session,
                f"MATCH (c)-[:{rel}]->(b) WHERE c.path IN $paths AND c.name IN $names AND b.path IS NOT NULL "
                "RETURN c.path AS c_path, c.name AS c_name, b.path AS b_path, b.name AS b_name",
                paths=sorted({p for p, _ in by_class}), names=sorted({c for _, c in by_class}),
            )
        except Exception:  # noqa: BLE001
            continue
    out: List[Dict[str, Any]] = []
    for b in bases:
        methods = by_class.get((b["c_path"], b["c_name"])) or []
        if not methods:
            continue
        try:
            rows = nb._rows(
                session,
                "MATCH (f:Function) WHERE f.path = $path AND f.class_context = $cls AND f.name IN $names "
                "RETURN f.name AS name, f.path AS path, f.line_number AS line, f.end_line AS end_line",
                path=b["b_path"], cls=b["b_name"], names=sorted({m["name"] for m in methods}),
            )
        except Exception:  # noqa: BLE001
            continue
        for row in rows:
            for m in methods:
                if m["name"] == row["name"]:
                    out.append(dict(row, kind="Function", cls=b["b_name"], overridden_by=m))
    return out


# Parse result keys -> graph labels (see the writer's item mappings).
_PARSE_KEYS = (("functions", "Function"), ("classes", "Class"), ("interfaces", "Interface"),
               ("structs", "Struct"), ("enums", "Enum"), ("records", "Record"), ("traits", "Trait"))
MAX_CODE_LINES = 150


def _norm_code(text: str) -> str:
    return " ".join(text.split())


def _span(lines: Sequence[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1:end])


def _parsed_entities(builder, repo: str, name: str, content: str) -> List[Dict[str, Any]]:
    """Entities of a file version that is not on disk (parsed from a copy)."""
    with tempfile.TemporaryDirectory(prefix="cviz-old-") as tmp:
        copy = Path(tmp) / Path(name).name
        copy.write_text(content, encoding="utf-8")
        try:
            data = builder.parse_file(Path(repo), copy)
        except Exception:  # noqa: BLE001 - an unparsable old version loses nothing we can show
            return []
    if data.get("error"):
        return []
    lines = content.splitlines()
    out = []
    for key, label in _PARSE_KEYS:
        for item in data.get(key) or []:
            line = nb._to_int(item.get("line_number"))
            if not item.get("name") or item["name"] == nb.MODULE_PSEUDO_FUNCTION or not line:
                continue
            end = nb._to_int(item.get("end_line")) or line
            out.append({"kind": label, "name": item["name"], "line": line, "end_line": end,
                        "cls": item.get("class_context") if label == "Function" else None,
                        "code": _span(lines, line, end)})
    return out


def _key3(e: Dict[str, Any]) -> Tuple[str, str, str]:
    return (e["kind"], e.get("cls") or "", e["name"])


def _lost_entities(builder, local_root: str, base: str, files: Sequence[FileChange],
                   current: Dict[str, List[Dict[str, Any]]],
                   old_keys: Dict[str, Counter]) -> List[Dict[str, Any]]:
    """Entities of the merge-base version of the changed files that the
    current version no longer has (by kind, class and name). *old_keys*
    receives, per file, what its old version declared."""
    lost: List[Dict[str, Any]] = []
    for f in files:
        if f.status == "added":
            continue
        if f.status == "modified" and not f.removals and all(pure for _, _, pure in f.ranges):
            continue  # only lines added: nothing can be gone
        old_rel = f.old_path or f.path
        try:
            content = _git(local_root, "show", f"{base}:./{old_rel}")
        except GitError:
            continue
        old = _parsed_entities(builder, local_root, old_rel, content)
        if not old and content.strip():
            continue  # not parsed: the diff lines decide (see FileChange.only_added)
        old_keys[f.path] = Counter(_key3(o) for o in old)
        now = Counter(_key3(e) for e in current.get(f.path, []))
        for o in sorted(old, key=lambda e: e["line"]):
            key = _key3(o)
            if now[key] > 0:
                now[key] -= 1
                continue
            lost.append(dict(o, old_rel=old_rel, file=f.path))
    return lost


def _match_lost(lost: List[Dict[str, Any]], added: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Moved and renamed entities: a lost entity found again among the added
    ones. Returns the lost entities left (deleted)."""
    left = list(lost)

    def take(d, a, **change):
        a.update(change, _matched=True)
        if a.get("file") != d["file"] or change.get("change") == "moved":
            a.setdefault("moved_from", f'{d["old_rel"]}:{d["line"]}')
        left.remove(d)

    for d in list(left):  # same kind and name: moved (to another file or class)
        same = [a for a in added if not a.get("_matched") and a["kind"] == d["kind"] and a["name"] == d["name"]]
        exact = [a for a in same if (a.get("cls") or "") == (d.get("cls") or "")]
        twin = [a for a in same if _norm_code(a.get("code", "")) == _norm_code(d["code"])]
        a = (exact or twin or [None])[0]
        if a is not None:
            take(d, a, change="moved", body_changed=_norm_code(a.get("code", "")) != _norm_code(d["code"]))
    for d in list(left):  # same code under another name: renamed
        for a in added:
            if a.get("_matched") or a["kind"] != d["kind"] or a["name"] == d["name"]:
                continue
            if _norm_code(d["code"].replace(d["name"], a["name"])) == _norm_code(a.get("code", "")):
                take(d, a, change="renamed", renamed_from=d["name"])
                break
    # A deleted type takes its deleted methods with it.
    types = {(d["old_rel"], d["name"]) for d in left if d["kind"] != "Function"}
    members = [d for d in left if d["kind"] == "Function" and (d["old_rel"], d.get("cls")) in types]
    for m in members:
        left.remove(m)
        owner = next(d for d in left if d["kind"] != "Function" and (d["old_rel"], d["name"]) == (m["old_rel"], m["cls"]))
        owner["members"] = owner.get("members", 0) + 1
    return left


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

MAX_REFS = 20       # references shown per deleted / renamed entity
TEST_DEPTH = 3      # call levels searched upwards for a test
_COMMENT_LINE = re.compile(r"^\s*(//|/\*|\*|#|--|')")
_PACKAGE_REF = re.compile(r'<PackageReference\b[^>]*?\bInclude\s*=\s*"([^"]+)"(?:[^>]*?\bVersion\s*=\s*"([^"]*)")?', re.I)
_PACKAGE_CFG = re.compile(r'<package\b[^>]*?\bid\s*=\s*"([^"]+)"(?:[^>]*?\bversion\s*=\s*"([^"]*)")?', re.I)
_PROJECT_FILES = ("*.csproj", "*.fsproj", "*.vbproj", "*.props", "packages.config")


def _references(local_root: str, name: str, kind: str, suffix: str) -> List[Tuple[str, int, str]]:
    """Lines of the working tree that use *name*: a call for a function, the
    word for a type. By name only: a homonym matches too."""
    try:
        files = _git(local_root, "grep", "-l", "-I", "-w", "-F", "--untracked", "-e", name,
                     "--", f"*{suffix}" if suffix else ".").splitlines()
    except GitError:  # also "no match" (exit code 1)
        return []
    word = re.escape(name)
    pattern = re.compile(rf"\b{word}\s*(<[^<>()]*>)?\s*\(" if kind == "Function" else rf"\b{word}\b")
    hits: List[Tuple[str, int, str]] = []
    for rel in files:
        try:
            lines = Path(local_root, rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, text in enumerate(lines, 1):
            if pattern.search(text) and not _COMMENT_LINE.match(text):
                hits.append((rel, number, text.strip()[:160]))
                if len(hits) >= MAX_REFS:
                    return hits
    return hits


def package_changes(local_root: str, base: str) -> List[Dict[str, Any]]:
    """PackageReference / packages.config entries added, removed or updated
    in the project files since the merge base."""
    try:
        out = _git(local_root, "diff", "-U0", "--relative", "--no-color", "--no-ext-diff", base, "--", *_PROJECT_FILES)
    except GitError:
        return []
    added: Dict[str, Tuple[str, str]] = {}
    removed: Dict[str, Tuple[str, str]] = {}
    current = ""
    for line in out.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            name = _unquote(line[4:].strip())
            if name.startswith("b/"):
                current = name[2:]
            elif line.startswith("--- ") and name.startswith("a/"):
                current = name[2:]
            continue
        if not line[:1] in "+-":
            continue
        for regex in (_PACKAGE_REF, _PACKAGE_CFG):
            for m in regex.finditer(line[1:]):
                (added if line[0] == "+" else removed)[m.group(1)] = (m.group(2) or "", current)
    changes_: List[Dict[str, Any]] = []
    for pkg, (version, file) in sorted(added.items()):
        if pkg in removed:
            if removed[pkg][0] != version:
                changes_.append({"package": pkg, "change": "updated", "version": version,
                                 "previous": removed[pkg][0], "file": file})
        else:
            changes_.append({"package": pkg, "change": "added", "version": version, "file": file})
    for pkg, (version, file) in sorted(removed.items()):
        if pkg not in added:
            changes_.append({"package": pkg, "change": "removed", "version": version, "file": file})
    return changes_


def _only_changed_externals(session, functions: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """External types the changed functions call and no other code does."""
    if not functions:
        return {}
    keys = {nb._key(f["path"], f["name"], f["line"]) for f in functions}
    try:
        rows = nb._rows(
            session,
            "MATCH (a:Function)-[:CALLS]->(e:ExternalClass) WHERE a.path IN $paths AND a.name IN $names "
            "RETURN a.path AS path, a.name AS name, a.line_number AS line, e.name AS ext",
            paths=sorted({f["path"] for f in functions}), names=sorted({f["name"] for f in functions}),
        )
    except Exception:  # noqa: BLE001
        return {}
    users: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if nb._key(r["path"], r["name"], r["line"]) in keys:
            users.setdefault(r["ext"], []).append(r)
    if not users:
        return {}
    try:
        all_rows = nb._rows(
            session,
            "MATCH (b)-[:CALLS]->(e:ExternalClass) WHERE e.name IN $names "
            "RETURN e.name AS ext, b.path AS path, b.name AS name, b.line_number AS line",
            names=sorted(users),
        )
    except Exception:  # noqa: BLE001
        return {}
    elsewhere = {r["ext"] for r in all_rows if nb._key(r["path"], r["name"], r["line"]) not in keys}
    return {ext: rows_ for ext, rows_ in users.items() if ext not in elsewhere}


def _untested(session, root: str, functions: Sequence[Dict[str, Any]],
              bases: Sequence[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Changed functions no test reaches within TEST_DEPTH call levels
    (through the interface methods they implement too). None when the
    repository has no test code at all."""
    try:
        paths = [r["path"] for r in nb._rows(session, "MATCH (f:File) WHERE f.path STARTS WITH $p RETURN f.path AS path",
                                             p=root + "/")]
    except Exception:  # noqa: BLE001
        return None
    if not any(nb.is_test_path(p) for p in paths):
        return None
    targets = [f for f in functions if not nb.is_test_path(f["path"])]
    owners: Dict[Tuple[str, str, Optional[int]], Set[int]] = {}
    frontier: List[Dict[str, Any]] = []
    for i, f in enumerate(targets):
        starts = [f] + [b for b in bases if b["overridden_by"] is f]
        for item in starts:
            key = nb._key(item["path"], item["name"], item["line"])
            if key not in owners:
                frontier.append(item)
            owners.setdefault(key, set()).add(i)
    covered: Set[int] = set()
    seen = set(owners)
    for _level in range(TEST_DEPTH):
        if not frontier:
            break
        nxt: List[Dict[str, Any]] = []
        for r in _caller_rows(session, frontier, nb.CALL_RELS):
            mine = owners.get(nb._key(r.get("a_path"), r.get("a_name"), r.get("a_line")), set())
            if nb.is_test_path(r.get("b_path")):
                covered |= mine
                continue
            key = nb._key(r.get("b_path"), r.get("b_name"), r.get("b_line"))
            owners.setdefault(key, set()).update(mine)
            if key not in seen and r.get("b_path"):
                seen.add(key)
                nxt.append({"path": r["b_path"], "name": r["b_name"], "line": r.get("b_line")})
        frontier = nxt
    return [f for i, f in enumerate(targets) if i not in covered]


def impact(session, repo_root: str, diff: Dict[str, Any], mapper: Optional[nb.PathMapper] = None,
           hide_framework: bool = False, hide_tests: bool = False,
           max_changed: int = MAX_CHANGED, callers_per_node: int = CALLERS_PER_NODE,
           builder=None, local_root: Optional[str] = None) -> Dict[str, Any]:
    """Nodes and links of the Changes view, in the format of nb.neighborhood."""
    mapper = mapper or nb.PathMapper()
    root = repo_root.replace("\\", "/").rstrip("/")
    files: List[FileChange] = diff["files"]
    by_path = {f"{root}/{f.path}": f for f in files if f.status != "deleted"}
    entities = _entities(session, sorted(by_path))

    changed: List[Dict[str, Any]] = []
    file_entities: Dict[str, int] = {}
    funcs_by_path: Dict[str, List[Dict[str, Any]]] = {}
    for e in entities:
        if e["kind"] == "Function":
            funcs_by_path.setdefault(e["path"], []).append(e)
    for e in entities:
        fc = by_path.get(e["path"])
        start, end = int(e["line"]), int(e.get("end_line") or e["line"])
        if not fc or not fc.touches(start, end):
            continue
        if e["kind"] != "Function" and fc.status != "added":
            # A type counts as changed when lines outside its methods changed
            # (fields, attributes, base types); its methods speak for the rest.
            inner = [(int(f["line"]), int(f.get("end_line") or f["line"]))
                     for f in funcs_by_path.get(e["path"], []) if start <= int(f["line"]) <= end]
            outside = FileChange(fc.path, fc.status,
                                 ranges=[r for r in fc.ranges if not any(a <= r[0] and r[1] <= b for a, b in inner)],
                                 removals=[x for x in fc.removals if not any(a <= x < b for a, b in inner)])
            if not outside.touches(start, end):
                continue
        e["change"] = "added" if fc.only_added(start, end) else "modified"
        e["file"] = fc.path
        changed.append(e)

    # What the changed files lost since the merge base: moved, renamed or deleted.
    lost: List[Dict[str, Any]] = []
    if builder is not None and local_root:
        current: Dict[str, List[Dict[str, Any]]] = {}
        for e in entities:
            current.setdefault(e["path"][len(root) + 1:], []).append(e)
        old_keys: Dict[str, Counter] = {}
        lost = _lost_entities(builder, local_root, diff["base"], files, current, old_keys)
        # New is what the old version of the file did not declare: a method
        # written where another one was is not "modified" because git paired
        # their lines in one hunk.
        for e in changed:
            if e["file"] in old_keys:
                e["change"] = "modified" if old_keys[e["file"]][_key3(e)] else "added"
        if lost:
            added = [e for e in changed if e["change"] == "added"]
            texts: Dict[str, List[str]] = {}
            for a in added:
                if a["file"] not in texts:
                    try:
                        texts[a["file"]] = Path(local_root, a["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
                    except OSError:
                        texts[a["file"]] = []
                a["code"] = _span(texts[a["file"]], int(a["line"]), int(a.get("end_line") or a["line"]))
            lost = _match_lost(lost, added)
            for a in added:
                a.pop("code", None)
                a.pop("_matched", None)
    for e in changed:
        file_entities[e["file"]] = file_entities.get(e["file"], 0) + 1
    for d in lost:
        file_entities[d["file"]] = file_entities.get(d["file"], 0) + 1
    changed.sort(key=lambda e: (e["path"], int(e["line"])))
    lost.sort(key=lambda d: (d["file"], d["line"]))
    hidden_changed = max(0, len(changed) + len(lost) - max_changed)
    shown = changed[:max_changed]
    shown_lost = lost[:max(0, max_changed - len(shown))]

    nodes: Dict[str, Dict[str, Any]] = {}
    links: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def add(kind, path, name, line, end_line, role, **extra) -> str:
        nid = nb.node_id(kind, path, name, nb._to_int(line))
        if nid not in nodes:
            nodes[nid] = {
                "id": nid, "kind": kind, "name": name, "label": nb._display_name(kind, name, path),
                "path": mapper.to_local(path), "indexed_path": path, "line": nb._to_int(line),
                "end_line": nb._to_int(end_line), "complexity": None, "role": role, "depth": 1,
                "external": not path, "hidden": {},
            }
        nodes[nid].update(extra)
        return nid

    changed_ids = {}
    for e in shown:
        extra = {k: e[k] for k in ("moved_from", "renamed_from", "body_changed") if k in e}
        nid = add(e["kind"], e["path"], e["name"], e["line"], e.get("end_line"), "changed",
                  change=e["change"], depth=0, file=e["file"], **extra)
        changed_ids[nb._key(e["path"], e["name"], e["line"])] = nid
    for d in shown_lost:
        # Not in the graph any more: no path to open, the old code instead.
        nid = "Deleted|" + nb.node_id(d["kind"], f'{root}/{d["old_rel"]}', d["name"], d["line"])
        code = d["code"].splitlines()
        nodes[nid] = {
            "id": nid, "kind": d["kind"], "name": d["name"], "label": d["name"], "path": None,
            "indexed_path": None, "line": d["line"], "end_line": d["end_line"], "complexity": None,
            "role": "changed", "change": "deleted", "depth": 0, "external": False, "hidden": {},
            "file": d["file"], "old_path": mapper.to_local(f'{root}/{d["old_rel"]}'), "old_rel": d["old_rel"],
            "code": "\n".join(code[:MAX_CODE_LINES]) + ("\n…" if len(code) > MAX_CODE_LINES else ""),
            "members": d.get("members", 0),
        }

    def link_callers(rows: List[Dict[str, Any]], family: str, role: str, **flags) -> None:
        per_target: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for r in rows:
            if hide_tests and nb.is_test_path(r.get("b_path")):
                continue
            tid = nb.node_id(r.get("a_kind") or "Function", r.get("a_path"), r.get("a_name"), nb._to_int(r.get("a_line")))
            if tid not in nodes:
                continue
            sid = nb.node_id(r.get("b_kind") or "Function", r.get("b_path"), r.get("b_name"), nb._to_int(r.get("b_line")))
            entry = per_target.setdefault(tid, {}).setdefault(sid, {"row": r, "lines": set(), "heuristic": True})
            if nb._to_int(r.get("call_line")) is not None:
                entry["lines"].add(nb._to_int(r["call_line"]))
            if r.get("rel") not in ("HEURISTIC_CALLS",):
                entry["heuristic"] = False
        for tid, sources in per_target.items():
            ordered = sorted(sources.items(), key=lambda kv: (kv[1]["row"].get("b_path") or "", kv[0]))
            count = 0
            for sid, entry in ordered:
                if sid == tid:
                    continue
                if sid not in nodes and (count >= callers_per_node or len(nodes) >= MAX_NODES):
                    nodes[tid]["hidden"][role] = nodes[tid]["hidden"].get(role, 0) + 1
                    continue
                r = entry["row"]
                if sid not in nodes:
                    add(r.get("b_kind") or "Function", r.get("b_path"), r.get("b_name"), r.get("b_line"),
                        r.get("b_end_line"), role)
                    count += 1
                lines = sorted(entry["lines"])
                links[(sid, tid, family)] = {
                    "source": sid, "target": tid, "type": family, "rel": r.get("rel"),
                    "line": lines[0] if lines else None, "lines": lines,
                    "heuristic": entry["heuristic"] if family == "CALLS" else False, **flags,
                }

    link_callers(_caller_rows(session, shown, nb.CALL_RELS), "CALLS", "caller")
    types = [e for e in shown if e["kind"] in _CLASS_LABELS]
    link_callers(_caller_rows(session, types, nb.INHERIT_RELS), "INHERITS", "child")

    # Interface / base methods the changed methods implement or override.
    bases = _base_methods(session, [e for e in shown if e["kind"] == "Function"])
    via = []
    for b in bases:
        if nb._key(b["path"], b["name"], b["line"]) in changed_ids:
            continue  # the base method changed too: it is already shown
        bid = add("Function", b["path"], b["name"], b["line"], b.get("end_line"), "parent",
                  via_type=b["cls"], label=f'{b["cls"]}.{b["name"]}')
        m = b["overridden_by"]
        mid = changed_ids[nb._key(m["path"], m["name"], m["line"])]
        links[(mid, bid, "INHERITS")] = {"source": mid, "target": bid, "type": "INHERITS", "rel": "OVERRIDES",
                                        "line": None, "lines": [], "heuristic": False}
        via.append(b)
    link_callers(_caller_rows(session, via, nb.CALL_RELS), "CALLS", "caller", via=True)

    alerts: List[Dict[str, Any]] = []
    # 1. Deleted or renamed, but its old name is still used.
    if local_root:
        gone = [(d, "Deleted|" + nb.node_id(d["kind"], f'{root}/{d["old_rel"]}', d["name"], d["line"]),
                 "deleted", d["name"], Path(d["old_rel"]).suffix) for d in shown_lost]
        gone += [(e, changed_ids[nb._key(e["path"], e["name"], e["line"])], "renamed", e["renamed_from"],
                  Path(e["file"]).suffix) for e in shown if e.get("change") == "renamed"]
        for entity, nid, how, name, suffix in gone:
            kind = entity["kind"]
            homonyms = [a for a in nb.resolve_symbol(session, name, mapper=mapper)
                        if (a["kind"] == "Function") == (kind == "Function")]
            declared = {(a["indexed_path"], a["line"]) for a in homonyms}
            refs = []
            for rel, line, text in _references(local_root, name, kind, suffix):
                ipath = f"{root}/{rel}"
                if (ipath, line) in declared:
                    continue
                try:
                    where = nb.locate(session, ipath, line, mapper)
                except nb.ArtifactNotFound:
                    where = None
                rid = None
                if where:
                    rid = add(where["kind"], where["indexed_path"], where["name"], where["line"],
                              where.get("end_line"), "caller")
                    nodes[rid].setdefault("uses_gone", []).append(f"{name} ({how})")
                    key = (rid, nid, "CALLS")
                    entry = links.setdefault(key, {"source": rid, "target": nid, "type": "CALLS", "rel": "REFERENCES",
                                                   "line": line, "lines": [], "heuristic": False, "broken": True})
                    entry["lines"].append(line)
                refs.append({"node": rid, "path": mapper.to_local(ipath), "rel": rel, "line": line, "text": text})
            if refs:
                alerts.append({
                    "type": "still_referenced", "severity": "maybe" if homonyms else "warning",
                    "title": f"{name} is {how}, its name is still used {len(refs)} time{'s' if len(refs) > 1 else ''}"
                             + (f" (or {len(homonyms)} homonym{'s' if len(homonyms) > 1 else ''} is meant)" if homonyms else ""),
                    "nodes": [nid] + [r["node"] for r in refs if r["node"]], "refs": refs,
                })
    # 2. New dependencies: packages of the project files, types only the changed code uses.
    if local_root:
        for pc in package_changes(local_root, diff["base"]):
            verb = {"added": "New package", "removed": "Package removed", "updated": "Package updated"}[pc["change"]]
            version = f'{pc["previous"]} → {pc["version"]}' if pc["change"] == "updated" else pc["version"]
            alerts.append({"type": f"package_{pc['change']}", "severity": "info",
                           "title": f"{verb}: {pc['package']} {version}".rstrip(), "detail": pc["file"], "nodes": []})
    live = [e for e in shown if e["kind"] == "Function"]
    externals = _only_changed_externals(session, live)
    ext_origins = nb._external_origins(session, externals)
    for ext, users in sorted(externals.items()):
        info = ext_origins.get(ext) or {}
        if info.get("origin") == "framework":
            continue
        where = info.get("package") or ("one of " + ", ".join(info.get("candidates") or []) if info.get("candidates") else
                                        "package not identified")
        names = sorted({u["name"] for u in users})
        alerts.append({"type": "new_external", "severity": "info",
                       "title": f"{ext} ({where}): only the changed code uses it",
                       "detail": "called by " + ", ".join(names),
                       "nodes": [changed_ids[nb._key(u["path"], u["name"], u["line"])] for u in users
                                 if nb._key(u["path"], u["name"], u["line"]) in changed_ids]})
    # 3. Changed code that no test reaches.
    untested = _untested(session, root, [e for e in live if e["change"] != "deleted"], bases)
    if untested:
        ids = [changed_ids[nb._key(f["path"], f["name"], f["line"])] for f in untested]
        for nid in ids:
            nodes[nid]["untested"] = True
        alerts.append({"type": "untested", "severity": "warning",
                       "title": f"{len(ids)} changed function{'s' if len(ids) > 1 else ''} no test reaches "
                                f"(within {TEST_DEPTH} calls)",
                       "detail": ", ".join(f["name"] for f in untested), "nodes": ids})

    # Whatever the per-node cap or the filters left out stays counted.
    origins = nb._external_origins(session, (n["name"] for n in nodes.values() if n["kind"] == "ExternalClass"))
    for n in nodes.values():
        if n["kind"] == "ExternalClass":
            n.update(origins.get(n["name"] or "") or {})
    comp = nb._complexities(session, [nb._key(n["indexed_path"], n["name"], n["line"])
                                      for n in nodes.values() if n["kind"] == "Function"])
    for n in nodes.values():
        if n["kind"] == "Function":
            n["complexity"] = comp.get(nb._key(n["indexed_path"], n["name"], n["line"]))

    counts = {"added": 0, "modified": 0, "moved": 0, "renamed": 0, "deleted": len(lost)}
    for e in changed:
        counts[e["change"]] += 1
    impacted = {nid for nid, n in nodes.items() if n["role"] in ("caller", "child")}
    file_list = [{
        "path": mapper.to_local(f"{root}/{f.path}"), "rel": f.path, "status": f.status, "old_path": f.old_path,
        "entities": file_entities.get(f.path, 0),
        "indexed": f.status == "deleted" or any(e["path"] == f"{root}/{f.path}" for e in entities),
    } for f in files]
    return {
        "nodes": list(nodes.values()),
        "links": list(links.values()),
        "files": file_list,
        "summary": {
            "files": len(files), **counts, "lost_detected": builder is not None,
            "deleted_files": sum(1 for f in files if f.status == "deleted"),
            "impacted": len(impacted), "hidden_changed": hidden_changed,
            "via_interfaces": len(via),
        },
        "alerts": alerts,
        "tests_found": untested is not None,
        "truncated": bool(hidden_changed) or any(n["hidden"] for n in nodes.values()),
    }


def compute(session, repo_root: str, target: str, mapper: Optional[nb.PathMapper] = None,
            **options) -> Dict[str, Any]:
    mapper = mapper or nb.PathMapper()
    local_root = mapper.to_local(repo_root) or repo_root
    started = time.perf_counter()
    diff = changes(local_root, target)
    data = impact(session, repo_root, diff, mapper, local_root=local_root, **options)
    data.update(target=target, base=diff["base"], base_short=diff["base_short"],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
    return data
