# Code Visualizer — a fork of CodeGraphContext

> [!IMPORTANT]
> **This repository is a fork.** It is based on
> [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext)
> (MIT License), created by Shashank Shekhar Singh and its contributors. All
> credit for the original engine (indexing, graph backends, MCP server, CLI)
> goes to them. This project is maintained separately at
> [polysok/code-visualizer](https://github.com/polysok/code-visualizer), is
> **not** affiliated with or endorsed by CodeGraphContext, and keeps no
> compatibility with it: report every issue here.

## What it is

**Code Visualizer is a desktop app** (macOS, Windows, Linux) that shows the
dependency graph of your repositories: callers, callees and inheritance of a
function, class or file, one tab per repository. It embeds everything it
needs: open a repository folder and it is indexed, then kept up to date as
you edit.

**IDE extensions complete it**, linking the graph to the code in both
directions: from the editor, open the artifact under the cursor in the graph;
from any node of the graph, jump back to its code in the IDE. An **MCP server**
gives AI coding agents the same graph.

### The desktop app

- **+** (or ⌘O / Ctrl+O) opens a repository in a new tab; the first time,
  it is indexed, with its progress shown. The tabs come back at the next
  start.
- The graph: depth 1–5, a click adds a node's neighbours, a double click
  recentres on it, heuristic edges can be hidden; the **Changes** view
  compares the current state with a target branch (added, modified, moved,
  renamed and deleted artifacts).
- Search by meaning in a chat box (see below), with the local embedding
  model included: nothing to install, nothing leaves the machine.
- *Open in Dependency Graph* in the IDE brings the app forward on the
  repository's tab, centred on the artifact under the cursor; a node's
  *Open in IDE* goes back to the code.
- A repository already served from a terminal or an IDE (`cviz graph`) is
  reused rather than opened twice.

### The IDE extensions

| IDE | Extension | Open in the graph |
| --- | --- | --- |
| VS Code, Cursor | `code-visualizer-vscode-X.Y.Z.vsix` (release files) | *CViz: Open in Dependency Graph*, `Cmd/Ctrl+Alt+G` |
| Rider, IntelliJ and the other JetBrains IDEs | External Tool, installed by `cviz ide jetbrains` (`extensions/rider/`) | *Tools › CViz › Open in Dependency Graph* |
| Zed | Task, installed by `cviz ide zed <repo>` (`extensions/zed/`) | *CViz: open in dependency graph* |

When the app runs, it comes forward on the repository's tab (opened, and
indexed, if needed), centred on the artifact under the cursor; installed but
not running (macOS), it is started first; otherwise the graph page opens in
the browser. `cviz graph` in a terminal does the same once its server
answers: the app's tab shows that server (`--no-browser` shows nothing; a
review, `--changes`, opens in the browser). VS Code: setting
`cviz.graphOpenIn` (`browser` to always use the browser); `cviz-open` and
`cviz graph`: `--browser` or `CVIZ_OPEN_IN=browser`. The JetBrains and Zed extensions run `cviz-open`:
they need the command line below.

### The MCP server

`cviz mcp start` is an MCP server that gives an AI coding agent five
read-only tools on the graph: `graph_overview`, `search_code`,
`traverse_graph` (callers, callees, inheritance, imports), `read_code` and
`execute_cypher_query`. It needs the command line (below). To configure an
agent:

```bash
# pick it from the list (Claude Code, Claude Desktop, Cursor, VS Code, Windsurf,
# Zed, Gemini CLI, Codex, Cline, JetBrains AI...): its MCP configuration is written
cviz mcp setup

# or by hand, Claude Code
claude mcp add code-visualizer -- cviz mcp start
```

Any other agent: `{"mcpServers": {"code-visualizer": {"command": "cviz",
"args": ["mcp", "start"]}}}` in its MCP configuration. `cviz mcp tools` lists
the tools.

**Which repository.** In this order:

1. `--repo <path>` (or the `CVIZ_REPO` variable): that repository, always.
   For a client without project folders, such as Claude Desktop (`cviz mcp
   setup` asks for it): `"args": ["mcp", "start", "--repo", "/path/to/repo"]`.
2. Else the first project folder the client announces (MCP *roots*, sent by
   VS Code, Cursor and other clients), followed when it changes.
3. Else the folder the client starts the server in (Claude Code, Codex,
   Gemini CLI: the folder they were started from).

The repository is the nearest folder with a `.code-visualizer/`, else the git
root. One server works on one repository at a time: for another one, start
the agent there (or use another `--repo`).

The MCP server never opens the database itself: it goes through the
repository's graph server (the one the app or `cviz graph` runs), so the
agent and the graph share one index, and starts one in the background when
none runs (indexing the repository the first time; `CVIZ_MCP_START_GRAPH=0`
makes it open the database instead).

## Under the hood

- **IDE links**: `cviz-open FILE:LINE` / `cviz open` open an artifact in the
  desktop app when it runs (it records a loopback URL and a token in
  `~/.code-visualizer/desktop.json`) or is installed (macOS: `open -b
  com.polysok.code-visualizer`), else in an open graph page, else in a new
  browser tab; nodes open the code in VS Code, Cursor, Rider, IntelliJ or
  Zed.
- **Live updates**: the watcher pushes changes to open pages over SSE, catches
  up on start-up with files changed while it was not running, re-links calls
  to symbols defined after their callers, and re-links callers after a rename.
- **More accurate call graph**: method calls are bound through the receiver's
  declared type in **C#, Rust, TypeScript and Swift**, so a call on a
  standard-library object (`Mutex`, `Map`, `NSLock`, `List`…) no longer links
  to a same-named project method; interface / trait / protocol calls go to an
  implementation, marked heuristic.
- **Natural-language search**: a chat box in the page finds code by meaning
  and by keyword (hybrid search, at most 5 results, each a link to its node);
  an optional LLM (LM Studio, Ollama, any OpenAI-compatible server) re-ranks
  the results and answers *how / why* questions. Vectors (local multilingual
  model or an embedding server) are computed by `cviz graph` at start-up for
  what is not vectorized yet, the live badge showing the progress; `cviz index
  --embed` or `cviz embed` compute them right away.
- **LadybugDB only**: the other graph backends of upstream (FalkorDB, KùzuDB,
  Neo4j, Nornic) are removed. LadybugDB is embedded (no server, no
  credentials) and one process opens the database at a time.
- **One index per repository**: each repository has its own database in
  `<repo>/.code-visualizer/` (ignored by Git) and its own `cviz graph` server
  (port 9324, or the next free one); the global database, named contexts and
  `cviz context` are removed.
- **HTTP API** for the page: `/api/locate`, `/api/neighborhood`,
  `/api/events`, `/api/focus`, `/api/open`, `/api/search`, `/api/chat`.

## Install

Each [release](https://github.com/polysok/code-visualizer/releases) has:

| File | What it is |
| --- | --- |
| `Code.Visualizer_X.Y.Z_aarch64.dmg`, `Code.Visualizer_X.Y.Z_x64-setup.exe`, `.deb`, `.AppImage` | The desktop app (below) |
| `install.sh`, `install.ps1` | One-line installers (below) |
| `code_visualizer-X.Y.Z-py3-none-any.whl` | The engine, for `uv tool install` (what the installers use) |
| `cviz-macos-arm64`, `cviz-linux-x86_64`, `cviz-windows.exe` | The engine as a single command-line program, no Python needed (run it from a terminal, it is not an app) |
| `code-visualizer-vscode-X.Y.Z.vsix` | The VS Code / Cursor extension (*Extensions › … › Install from VSIX*) |

**The app**: download the `.dmg` (macOS), the `-setup.exe` (Windows) or
the `.deb` / `.AppImage` (Linux) and install it; then the extension of your
IDE. The builds are not signed with an Apple or Microsoft certificate:
macOS says on first launch that it cannot verify the app, allow it once in
*System Settings › Privacy & Security › Open Anyway* (if macOS says the app
is "damaged", run `xattr -dr com.apple.quarantine "/Applications/Code Visualizer.app"`); Windows shows a SmartScreen warning (*More info ›
Run anyway*).

**The command line** (`cviz`, `cviz-open`; package `code-visualizer`, not
on PyPI), for the JetBrains and Zed extensions, the MCP server, a terminal or
a remote machine; from the folder of the
repository to look at:

```bash
# macOS, Linux
curl -LsSf https://github.com/polysok/code-visualizer/releases/latest/download/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://github.com/polysok/code-visualizer/releases/latest/download/install.ps1 | iex
```

The installer sets up [uv](https://docs.astral.sh/uv/) if needed (no admin
rights, no Python required), installs `cviz` with the local embedding model,
and offers to open the graph of the current repository. Run it again to
upgrade. Afterwards, `cviz graph --watch .` in a repository opens its graph
(http://localhost:9324/graph), indexing it first if needed.

A large repository is indexed in two stages the first time: the page opens
once the first 1,000 files are indexed (code before tests and generated
files, shallow folders before deep ones), and the rest is indexed in the
background, the live badge showing `indexing X / Y`; links between the two
stages are resolved when the second ends. `cviz config set
GRAPH_FIRST_PASS_FILES 3000` changes the size of the first stage, `0` indexes
everything before opening the page. `cviz index` always indexes everything.

By hand, the same thing:

```bash
uv tool install 'code-visualizer[semantic] @ https://github.com/polysok/code-visualizer/releases/download/vX.Y.Z/code_visualizer-X.Y.Z-py3-none-any.whl'
cviz graph --watch /path/to/repo      # http://localhost:9324/graph
```

## License

MIT — see [LICENSE](LICENSE).
