"""Which ``cviz graph`` server serves which repository.

Every repository has its own database, so several graph servers can run at
once, one per repository, each on its own port. Each server records itself
in ``~/.code-visualizer/servers.json`` (``{repo root: {url, pid}}``) while
it runs; ``cviz-open``, ``cviz update`` (Git hooks) and the IDE extensions look
up the server of the file or folder they work on.

Standard library only: ``cviz-open`` imports it on every keyboard shortcut.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Dict, Iterator, Optional

LOCAL_DIR_NAME = ".code-visualizer"


def registry_file() -> Path:
    custom = os.environ.get("CVIZ_SERVERS_FILE")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".code-visualizer" / "servers.json"


# What only the user-level folder (~/.code-visualizer) holds; a repository's
# index folder has none of these.
USER_DIR_MARKERS = ("global", "models", "servers.json", "events.jsonl", ".first_run_done")


def is_user_config_dir(path: Path) -> bool:
    """True for the user-level settings folder, which is never a repository's index.

    Checked by content as well as by location: with HOME overridden (tests,
    sandboxes) the real one is no longer at ``Path.home()``.
    """
    resolved = path.resolve()
    home = Path.home()
    if resolved == (home / LOCAL_DIR_NAME).resolve():
        return True
    return any((path / marker).exists() for marker in USER_DIR_MARKERS)


def find_repo_root(start) -> Path:
    """Same rule as config_manager.find_repo_root (kept import-light here):
    nearest folder with a ``.code-visualizer/``, else the git root, else the folder."""
    current = Path(start).expanduser().resolve()
    if current.is_file() or (not current.exists() and current.suffix):
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / LOCAL_DIR_NAME
        if candidate.is_dir() and not is_user_config_dir(candidate):
            return directory
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory
    return current


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read() -> Dict[str, dict]:
    try:
        data = json.loads(registry_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(entries: Dict[str, dict]) -> None:
    path = registry_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".servers-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Serialise read-modify-write of the registry across processes (servers
    of several repositories start at the same time)."""
    path = registry_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path.with_name(path.name + ".lock"), "a+")
    except OSError:
        yield
        return
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # Windows
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        handle.close()  # releases the lock


def servers() -> Dict[str, dict]:
    """Running servers by repository root (entries of dead processes dropped)."""
    entries = _read()
    return {repo: e for repo, e in entries.items() if isinstance(e, dict) and _pid_alive(e.get("pid"))}


def register(repo_root, url: str, pid: Optional[int] = None) -> None:
    with _locked():
        entries = {r: e for r, e in _read().items() if isinstance(e, dict) and _pid_alive(e.get("pid"))}
        entries[Path(repo_root).resolve().as_posix()] = {"url": url.rstrip("/"), "pid": pid or os.getpid()}
        _write(entries)


def unregister(repo_root, pid: Optional[int] = None) -> None:
    with _locked():
        entries = _read()
        key = Path(repo_root).resolve().as_posix()
        entry = entries.get(key)
        if entry and int(entry.get("pid", -1)) == (pid or os.getpid()):
            del entries[key]
            _write(entries)


def server_for(path) -> Optional[str]:
    """URL of the running server whose repository contains *path*."""
    target = Path(path).expanduser().resolve().as_posix()
    best = None
    for repo, entry in servers().items():
        if target == repo or target.startswith(repo.rstrip("/") + "/"):
            if best is None or len(repo) > len(best[0]):
                best = (repo, entry.get("url"))
    return best[1] if best else None


def claim_port(repo_root, start: int, host: str = "127.0.0.1", public_host: str = "localhost",
               attempts: int = 100) -> int:
    """Pick the server port of *repo_root* and record it, atomically.

    Under the registry lock: the first port from *start* that is neither used
    by another repository's running server nor bound by another program.
    Two servers starting at the same time cannot pick the same port.
    """
    key = Path(repo_root).resolve().as_posix()
    with _locked():
        entries = {r: e for r, e in _read().items() if isinstance(e, dict) and _pid_alive(e.get("pid"))}
        taken = set()
        for repo, entry in entries.items():
            if repo == key:
                continue
            try:
                taken.add(int(str(entry.get("url", "")).rsplit(":", 1)[1]))
            except (IndexError, ValueError):
                pass
        port = start
        for candidate in range(start, start + attempts):
            if candidate not in taken and port_free(candidate, host):
                port = candidate
                break
        entries[key] = {"url": f"http://{public_host}:{port}", "pid": os.getpid()}
        _write(entries)
    return port


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def free_port(start: int, host: str = "127.0.0.1", attempts: int = 100) -> int:
    """*start*, or the next free port above it (one server per repository)."""
    for port in range(start, start + attempts):
        if port_free(port, host):
            return port
    return start
