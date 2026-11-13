# Code Visualizer desktop

A window with one tab per repository, each showing that repository's
dependency graph (the `/graph` page of its `cviz graph` server). The app
embeds the `cviz` program: nothing else to install.

- **+** (or ⌘O / Ctrl+O): open a repository. Its server starts (the first
  time, the repository is indexed), or the one already running — started
  from a terminal or an IDE — is reused.
- ⌘1…⌘9 / Ctrl+1…9: go to a tab; Ctrl+Tab: next tab.
- Closing a tab stops its server if the app started it; quitting the app
  stops all of those. The tabs come back at the next start (only the active
  one starts its server right away).
- *Open in IDE* in a graph page opens the IDE through the system.

## Build

Needs Rust and Node, and the `cviz` binary of the platform, built from
`cviz.spec` (the release workflow builds it):

```bash
# from the repository root: the cviz binary (dist/cviz) and the grammars
pyinstaller cviz.spec --clean
TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=desktop/src-tauri/grammars python scripts/prefetch_grammars.py
cd desktop
mkdir -p src-tauri/binaries
cp ../dist/cviz src-tauri/binaries/cviz-$(rustc -vV | sed -n 's/^host: //p')
npm ci
npx tauri dev        # or: npx tauri build
```

The graph page is served by the embedded `cviz`: after a change to the page
(`src/code_visualizer/viz/static`), rebuild the binary, or the app keeps the
old page. The release workflow builds both from the same commit.

The tree-sitter grammars are shipped with the app (`grammars/`, a resource)
and copied to its cache folder on first use, so that a first index needs no
network; `cviz` downloads there the grammars of other languages.

`CVIZ_DESKTOP_CVIZ=/path/to/cviz` makes the app use another `cviz`.

The builds are not signed: macOS asks to allow the app once (System
Settings › Privacy & Security › *Open Anyway*), Windows shows a SmartScreen
warning (*More info* › *Run anyway*).
