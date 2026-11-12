"""Companion sources of a repository: other repositories or folders of code
indexed together with it so that its calls into them become real links.

A C# solution calls into packages whose sources usually live in other
repositories of the same team (``Septeo.Brain.Model`` ...). Without them those
calls end on ``ExternalClass`` nodes, often with an ambiguous package. Indexed
in the same pass, their types are ordinary classes: calls, inheritance and
"open in IDE" work across the repositories.

The list belongs to the developer's machine (paths are local), so it is kept
next to the repository's database, in ``<repo>/.code-visualizer/companions.json``
(the folder is not versioned):

    {"sources": [{"path": "/abs/Septeo.Brain.Model", "added_at": "..."}],
     "indexed": ["/abs/Septeo.Brain.Model"], "indexed_at": "..."}

``sources`` is what the developer wants; ``indexed`` is what the graph holds
since the last full index. They differ until the next re-index ("pending").
Companions are written as dependency repositories (``is_dependency``): their
own unused code is not reported as dead code of the repository.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

CONFIG_DIR_NAME = ".code-visualizer"
CONFIG_FILE_NAME = "companions.json"

_lock = threading.Lock()


class CompanionError(ValueError):
    """A companion source that cannot be added (the message says why)."""


def _posix(path: Path | str) -> str:
    return Path(path).expanduser().resolve().as_posix()


def config_file(repo_root: Path | str) -> Path:
    return Path(repo_root) / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def _load(repo_root: Path | str) -> Dict[str, Any]:
    try:
        data = json.loads(config_file(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["sources"] = [s for s in data.get("sources", []) if isinstance(s, dict) and s.get("path")]
    data["indexed"] = [p for p in data.get("indexed", []) if isinstance(p, str)]
    return data


def _save(repo_root: Path | str, data: Dict[str, Any]) -> None:
    path = config_file(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# Code Visualizer index of this repository: not versioned\n*\n", encoding="utf-8")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def companion_roots(repo_root: Path | str) -> List[Path]:
    """The companion folders to index with *repo_root* (missing ones left out)."""
    roots = [Path(s["path"]) for s in _load(repo_root)["sources"]]
    return [r for r in roots if r.is_dir()]


def indexed_roots(repo_root: Path | str) -> List[str]:
    """Companions the graph holds since the last full index."""
    return list(_load(repo_root)["indexed"])


def record_indexed(repo_root: Path | str, roots: Iterable[Path | str]) -> None:
    with _lock:
        data = _load(repo_root)
        data["indexed"] = sorted({_posix(r) for r in roots})
        data["indexed_at"] = datetime.now(timezone.utc).isoformat()
        _save(repo_root, data)


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def add(repo_root: Path | str, path: str) -> str:
    """Add *path* (absolute, or relative to the repository) as a companion."""
    raw = (path or "").strip()
    if not raw:
        raise CompanionError("Give the path of a repository or folder.")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(repo_root) / candidate
    if not candidate.exists():
        raise CompanionError(f"{raw} does not exist on this machine.")
    if not candidate.is_dir():
        raise CompanionError(f"{raw} is a file: give the folder that contains the code.")
    target = _posix(candidate)
    main = _posix(repo_root)
    if target in (Path.home().resolve().as_posix(), Path(target).anchor.replace("\\", "/")):
        raise CompanionError(f"{raw} is too wide: give the folder of a repository.")
    if _overlaps(target, main):
        raise CompanionError(
            f"{raw} is {'this repository' if target == main else 'inside or around this repository'}: "
            "its code is already indexed.")
    with _lock:
        data = _load(repo_root)
        for source in data["sources"]:
            if _overlaps(target, _posix(source["path"])):
                raise CompanionError(f"{raw} is already covered by {source['path']}.")
        data["sources"].append({"path": target, "added_at": datetime.now(timezone.utc).isoformat()})
        _save(repo_root, data)
    return target


def remove(repo_root: Path | str, path: str) -> bool:
    target = (path or "").strip()
    with _lock:
        data = _load(repo_root)
        keep = [s for s in data["sources"]
                if s["path"] != target and _posix(s["path"]) != _posix(target or ".")]
        if len(keep) == len(data["sources"]):
            return False
        data["sources"] = keep
        _save(repo_root, data)
    return True


def kind_of(path: Path) -> str:
    return "repository" if (path / ".git").exists() else "folder"


def describe(repo_root: Path | str) -> Dict[str, Any]:
    """The list as the page shows it: one entry per source, plus what is
    pending until the next re-index."""
    data = _load(repo_root)
    indexed = set(data["indexed"])
    wanted = set()
    sources = []
    for source in data["sources"]:
        p = Path(source["path"])
        posix = _posix(p)
        wanted.add(posix)
        sources.append({
            "path": posix,
            "name": p.name,
            "kind": kind_of(p) if p.is_dir() else "missing",
            "exists": p.is_dir(),
            "indexed": posix in indexed,
            "added_at": source.get("added_at"),
        })
    removed = sorted(indexed - wanted)
    return {
        "sources": sources,
        "removed_pending": removed,
        "pending": bool(removed) or any(s["exists"] and not s["indexed"] for s in sources),
        "indexed_at": data.get("indexed_at"),
    }


# ---------------------------------------------------------------------------
# Suggestions: sibling folders that provide packages the repository uses
# ---------------------------------------------------------------------------

_SKIP_DIRS = {"node_modules", "bin", "obj", "packages", ".git", ".vs", ".idea", "dist", "build", "target"}


def _project_names(root: Path, max_depth: int = 4, cap: int = 400) -> set:
    """Names of the .csproj files under *root* (the assembly / package names)."""
    names: set = set()
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")] \
            if depth < max_depth else []
        for f in filenames:
            if f.endswith(".csproj"):
                names.add(f[:-len(".csproj")])
                if len(names) >= cap:
                    return names
    return names


def suggestions(repo_root: Path | str, limit: int = 8) -> List[Dict[str, Any]]:
    """Folders next to the repository that look like repositories, the ones
    providing packages it references first."""
    main = Path(repo_root).resolve()
    parent = main.parent
    taken = {_posix(s["path"]) for s in _load(main)["sources"]}
    try:
        from ..tools.indexing.dotnet_packages import load_projects

        used = {name for project in load_projects(main) for name in project.packages}
    except Exception:  # noqa: BLE001 - suggestions are a convenience
        used = set()
    used_lower = {u.lower() for u in used}

    out = []
    try:
        siblings = sorted(p for p in parent.iterdir() if p.is_dir() and not p.name.startswith("."))[:300]
    except OSError:
        return []
    for sibling in siblings:
        posix = sibling.resolve().as_posix()
        if posix == main.as_posix() or posix in taken:
            continue
        is_repo = (sibling / ".git").exists()
        provides = sorted(n for n in _project_names(sibling) if n.lower() in used_lower) if used else []
        if not provides and not is_repo:
            continue
        out.append({"path": posix, "name": sibling.name, "kind": kind_of(sibling), "provides": provides})
    out.sort(key=lambda s: (-len(s["provides"]), s["name"].lower()))
    return [s for s in out if s["provides"]][:limit] or out[:limit]


def display_path(path: str, repo_root: Path | str) -> str:
    """``../Septeo.Brain.Model`` for a sibling of the repository, else the path."""
    try:
        return Path(os.path.relpath(path, Path(repo_root).resolve())).as_posix()
    except ValueError:  # other drive on Windows
        return path


def optional_roots(repo_root: Optional[Path | str]) -> List[Path]:
    """companion_roots, or none when the repository is unknown or unreadable."""
    if not repo_root:
        return []
    try:
        return companion_roots(repo_root)
    except Exception:  # noqa: BLE001
        return []
