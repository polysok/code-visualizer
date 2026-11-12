"""``cviz-open FILE[:LINE]`` — show an artifact in the dependency-graph page.

The IDE-side half of the IDE <-> web integration. Every editor integration
(VS Code command, Rider External Tool, Zed task) ends up here or does the same
two steps:

1. ``POST /api/focus`` so a graph tab that is already open recentres on the
   artifact (no tab pile-up);
2. if no tab is listening, open ``/graph?path=…&line=…`` in the browser.

When the server is not running it is started in the background
(``cviz graph --no-browser``) unless ``--no-start`` or ``CVIZ_OPEN_NO_START=1``.

Deliberately stdlib-only and import-light: it runs on every keyboard shortcut.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple

# Default port of the graph server (cviz graph / cviz visualize). Not 8000:
# too many local dev servers already use it.
DEFAULT_PORT = 9324
DEFAULT_SERVER = f"http://localhost:{DEFAULT_PORT}"

_LOCATION_RE = re.compile(r"^(?P<path>.+?)(?::(?P<line>\d+))?(?::(?P<col>\d+))?$")


def parse_location(spec: str) -> Tuple[str, Optional[int]]:
    """Split ``path[:line[:col]]``; Windows drive letters (``C:\\x``) are kept."""
    spec = spec.strip().strip('"')
    m = _LOCATION_RE.match(spec)
    if not m:
        return spec, None
    path, line = m.group("path"), m.group("line")
    # "C:" alone is a drive, not "path C, line ..." — the regex never splits
    # there because a drive is followed by a separator, not digits.
    return path, int(line) if line else None


def graph_url(server: str, path: str, line: Optional[int], depth: Optional[int] = None,
              ide: Optional[str] = None) -> str:
    params = {"path": path}
    if line:
        params["line"] = str(line)
    if depth and depth != 1:
        params["depth"] = str(depth)
    if ide:
        params["ide"] = ide
    return f"{server.rstrip('/')}/graph?{urllib.parse.urlencode(params)}"


def _request(url: str, payload: Optional[dict] = None, timeout: float = 2.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    key = os.getenv("CVIZ_API_KEY")
    if key:
        req.add_header("X-API-Key", key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local URL
        body = resp.read().decode("utf-8") or "{}"
    return json.loads(body)


def server_alive(server: str) -> bool:
    try:
        _request(f"{server.rstrip('/')}/api/graph-config", timeout=1.0)
        return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500  # it answered, so it is up (e.g. 401/403)
    except Exception:  # noqa: BLE001
        return False


def focus_existing(server: str, path: str, line: Optional[int]) -> int:
    """Ask open graph tabs to recentre; returns how many received it."""
    try:
        return int(_request(f"{server.rstrip('/')}/api/focus", {"path": path, "line": line}).get("delivered", 0))
    except Exception:  # noqa: BLE001
        return 0


def _cviz_command() -> List[str]:
    exe = shutil.which("cviz")
    if exe:
        return [exe]
    return [sys.executable, "-m", "code_visualizer"]


def start_server(repo_root: Path, timeout: float = 120.0) -> Optional[str]:
    """Start ``cviz graph --watch <repo>`` in the background; its URL once it answers.

    The server picks a free port and records itself in the server registry,
    which is how its URL is found.
    """
    from . import server_registry

    cmd = _cviz_command() + ["graph", "--no-browser", "--watch", str(repo_root)]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  cwd=str(repo_root))
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)  # noqa: S603 - fixed argv
    except OSError:
        return None
    deadline = time.time() + timeout  # a first run indexes the repository
    while time.time() < deadline:
        url = server_registry.server_for(repo_root)
        if url and server_alive(url):
            return url
        time.sleep(0.5)
    return None


def resolve_server(path: str, explicit: Optional[str] = None) -> Tuple[str, bool]:
    """``(url, alive)`` of the graph server for *path*.

    An explicit URL (--server, CVIZ_GRAPH_URL) wins; else the running server
    of the repository containing *path* (server registry); else the default.
    """
    if explicit:
        return explicit.rstrip("/"), server_alive(explicit)
    from . import server_registry

    url = server_registry.server_for(path)
    if url and server_alive(url):
        return url, True
    return DEFAULT_SERVER, False


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cviz-open",
        description="Show the artifact at FILE[:LINE] in the CViz dependency-graph page.",
    )
    parser.add_argument("location", help="FILE, FILE:LINE or FILE:LINE:COLUMN")
    parser.add_argument("line", nargs="?", type=int, help="Line number (alternative to FILE:LINE)")
    parser.add_argument("--server", default=os.getenv("CVIZ_GRAPH_URL"),
                        help="Graph server URL (env CVIZ_GRAPH_URL; default: the server of the "
                             "file's repository, started if needed)")
    parser.add_argument("--depth", type=int, default=None, help="Initial depth (1-5)")
    parser.add_argument("--ide", default=os.getenv("CVIZ_DEFAULT_IDE"),
                        help="IDE the page should open code in (vscode, cursor, rider, idea, zed)")
    parser.add_argument("--new-tab", action="store_true", help="Always open a new tab")
    parser.add_argument("--no-start", action="store_true",
                        help="Do not start 'cviz graph' when the server is not running")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="Only print the URL")
    args = parser.parse_args(argv)

    path_str, line = parse_location(args.location)
    if args.line:
        line = args.line
    path = str(Path(path_str).expanduser().resolve())
    server, alive = resolve_server(path, args.server)

    if args.print_only:
        print(graph_url(server, path, line, args.depth, args.ide))
        return 0

    if not alive:
        from .server_registry import find_repo_root

        no_start = args.no_start or os.getenv("CVIZ_OPEN_NO_START", "").lower() in ("1", "true", "yes")
        repo_root = find_repo_root(path)
        started = None if (no_start or args.server) else start_server(repo_root)
        if not started:
            print(f"cviz-open: no graph server for {repo_root}. Start it with: "
                  f"cd {repo_root} && cviz graph --watch .", file=sys.stderr)
            return 2
        server = started
    url = graph_url(server, path, line, args.depth, args.ide)

    if not args.new_tab and focus_existing(server, path, line) > 0:
        print(f"cviz-open: focused the open graph tab on {path}:{line or ''}")
        return 0

    webbrowser.open(url, new=2)
    print(f"cviz-open: {url}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
