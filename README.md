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

## What this fork adds

The goal is to navigate an application's dependency graph from a web page
linked to the IDE in both directions: open the artifact under the cursor from
VS Code, Rider or Zed, and jump back to the code from any node of the graph.

- **Dependency-graph page** (`cviz graph [--watch <repo>]`, served at
  `/graph`): callers, callees and inheritance of a function, class or file,
  depth 1–5. A click adds a node's neighbours to the current graph; a double
  click recentres on it; nodes open the code in the IDE (VS Code, Cursor,
  Rider, IntelliJ, Zed). Heuristic edges can be hidden.
- **IDE integrations**: `cviz-open FILE:LINE` / `cviz open`, the VS Code command
  *CViz: Open in Dependency Graph* (`Cmd/Ctrl+Alt+G`), a Rider External Tool
  (`extensions/rider/`) and a Zed task (`extensions/zed/`).
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

### Install this fork

The fork is distributed as `code-visualizer` (not on PyPI): import package
`code_visualizer`, commands `cviz` and `cviz-open`. Each
[release](https://github.com/polysok/code-visualizer/releases) has:

| File | What it is |
| --- | --- |
| `install.sh`, `install.ps1` | One-line installers (below) |
| `code_visualizer-X.Y.Z-py3-none-any.whl` | The engine, for `uv tool install` (what the installers use) |
| `cviz-macos-arm64`, `cviz-linux-x86_64`, `cviz-windows.exe` | The engine as a single command-line program, no Python needed (run it from a terminal, it is not an app) |
| `code-visualizer-vscode-X.Y.Z.vsix` | The VS Code / Cursor extension |

From the folder of the repository to look at:

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

By hand, the same thing:

```bash
uv tool install 'code-visualizer[semantic] @ https://github.com/polysok/code-visualizer/releases/download/vX.Y.Z/code_visualizer-X.Y.Z-py3-none-any.whl'
cviz graph --watch /path/to/repo      # http://localhost:9324/graph
```

## License

MIT — see [LICENSE](LICENSE).
