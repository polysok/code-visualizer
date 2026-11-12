"""`cviz ide ...`: install the IDE side of the dependency-graph integration.

* ``cviz ide zed [REPO]`` adds the "open in dependency graph" task to the
  repository's ``.zed/tasks.json`` (created if missing, existing tasks and
  comments kept) and prints the key binding to add to the user keymap.
* ``cviz ide jetbrains`` writes the External Tool ``tools/CViz.xml`` into every
  JetBrains IDE configuration directory found (Rider, IntelliJ, WebStorm…),
  pointing at the absolute path of ``cviz-open`` so it works even when the IDE
  is started from the Dock / Start menu without the shell PATH.

Both are idempotent: running them again changes nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from xml.sax.saxutils import quoteattr

ZED_TASK_LABEL = "CViz: open in dependency graph"
JETBRAINS_TOOL_FILE = "CViz.xml"

ZED_KEYMAP_SNIPPET = """[
  {
    "context": "Editor",
    "bindings": {
      "cmd-alt-g": ["task::Spawn", { "task_name": "CViz: open in dependency graph" }],
      "ctrl-alt-g": ["task::Spawn", { "task_name": "CViz: open in dependency graph" }]
    }
  }
]"""


# ---------------------------------------------------------------------------
# Command resolution
# ---------------------------------------------------------------------------


def launcher(absolute: bool) -> Tuple[str, List[str]]:
    """(program, leading args) that open FILE:LINE in the graph page.

    Prefers the ``cviz-open`` script; falls back to ``cviz open`` (the frozen
    binary only ships ``cviz``). With ``absolute`` the program is resolved to a
    full path, for IDEs that do not see the shell PATH.
    """
    for program, lead in (("cviz-open", []), ("cviz", ["open"])):
        found = shutil.which(program)
        if found:
            # Keep symlinks (e.g. ~/.local/bin/cviz-open from `uv tool`): their
            # target moves on upgrade, the link does not.
            return (str(Path(found).absolute()) if absolute else program), lead
    # Nothing on PATH (e.g. running from a source checkout): use this
    # interpreter, which can always run the launcher module.
    if absolute:
        return sys.executable, ["-m", "code_visualizer.cli.open_in_graph"]
    return "cviz-open", []


# ---------------------------------------------------------------------------
# Zed
# ---------------------------------------------------------------------------


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments outside strings (Zed files are JSONC)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def zed_task(program: str, lead: Sequence[str]) -> dict:
    return {
        "label": ZED_TASK_LABEL,
        "command": program,
        "args": [*lead, "$ZED_FILE:$ZED_ROW", "--ide", "zed"],
        "reveal": "never",
        "hide": "on_success",
        "allow_concurrent_runs": True,
    }


def install_zed_task(repo: Path, program: str, lead: Sequence[str]) -> str:
    """Add the task to ``repo/.zed/tasks.json``; returns what was done."""
    tasks_file = repo / ".zed" / "tasks.json"
    task_json = json.dumps(zed_task(program, lead), indent=2)
    if not tasks_file.exists():
        tasks_file.parent.mkdir(parents=True, exist_ok=True)
        indented = "\n".join("  " + line for line in task_json.splitlines())
        tasks_file.write_text(
            "// Zed tasks for this repository. The CViz task opens the artifact under the\n"
            "// cursor in the dependency-graph page (Code Visualizer).\n"
            f"[\n{indented}\n]\n",
            encoding="utf-8",
        )
        return f"created {tasks_file}"

    text = tasks_file.read_text(encoding="utf-8")
    stripped = _strip_jsonc(text)
    try:
        existing = json.loads(re.sub(r",(\s*[\]}])", r"\1", stripped))  # tolerate trailing commas
    except json.JSONDecodeError as exc:
        raise ValueError(f"{tasks_file} is not valid JSON(C): {exc}") from exc
    if not isinstance(existing, list):
        raise ValueError(f"{tasks_file} must contain a JSON array of tasks")
    if any(isinstance(t, dict) and t.get("label") == ZED_TASK_LABEL for t in existing):
        return f"already present in {tasks_file}"

    # Insert textually before the closing bracket so comments survive.
    close = text.rstrip().rfind("]")
    if close == -1:
        raise ValueError(f"{tasks_file}: closing ']' not found")
    indented = "\n".join("  " + line for line in task_json.splitlines())
    separator = ",\n" if existing else "\n"
    before = text[:close].rstrip()
    if existing and before.endswith(","):
        separator = "\n"
    tasks_file.write_text(f"{before}{separator}{indented}\n{text[close:]}", encoding="utf-8")
    return f"added to {tasks_file}"


# ---------------------------------------------------------------------------
# JetBrains
# ---------------------------------------------------------------------------

_JETBRAINS_PRODUCT = re.compile(
    r"^(Rider|IntelliJIdea|IdeaIC|WebStorm|PyCharm|PyCharmCE|GoLand|CLion|PhpStorm|RubyMine|"
    r"DataGrip|RustRover|Aqua|DataSpell|Fleet)\d{4}\.\d+$"
)


def jetbrains_config_roots() -> List[Path]:
    home = Path.home()
    roots = []
    if sys.platform == "darwin":
        roots.append(home / "Library" / "Application Support" / "JetBrains")
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "JetBrains")
    else:
        roots.append(Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "JetBrains")
    return roots


def jetbrains_config_dirs(product: Optional[str] = None) -> List[Path]:
    """Per-version IDE configuration directories, e.g. ``.../JetBrains/Rider2025.2``."""
    found: List[Path] = []
    for root in jetbrains_config_roots():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and _JETBRAINS_PRODUCT.match(child.name):
                if product and not child.name.lower().startswith(product.lower()):
                    continue
                found.append(child)
    return found


def ide_for_config_dir(config_dir: Path) -> str:
    """Value of the page's `ide` parameter for an IDE configuration directory."""
    return "rider" if config_dir.name.lower().startswith("rider") else "idea"


def jetbrains_tool_xml(program: str, lead: Sequence[str], ide: str = "rider") -> str:
    params = " ".join([*lead, '"$FilePath$:$LineNumber$"', "--ide", ide])
    return f"""<toolSet name="CViz">
  <tool name="Open in Dependency Graph" description="Show the artifact under the caret in the CViz dependency graph" showInMainMenu="true" showInEditor="true" showInProject="true" showInSearchPopup="true" disabled="false" useConsole="false" showConsoleOnStdOut="false" showConsoleOnStdErr="true" synchronizeAfterRun="false">
    <exec>
      <option name="COMMAND" value={quoteattr(program)} />
      <option name="PARAMETERS" value={quoteattr(params)} />
      <option name="WORKING_DIRECTORY" value="$ProjectFileDir$" />
    </exec>
  </tool>
</toolSet>
"""


def install_jetbrains_tool(config_dir: Path, program: str, lead: Sequence[str],
                           dry_run: bool = False) -> str:
    target = config_dir / "tools" / JETBRAINS_TOOL_FILE
    content = jetbrains_tool_xml(program, lead, ide_for_config_dir(config_dir))
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return f"up to date: {target}"
    if dry_run:
        return f"would write {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {target}"
