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
- From the IDE (*Open in Dependency Graph*: VS Code extension, `cviz-open`),
  the app comes forward on the repository's tab, opened if needed, centred
  on the artifact. The app records how to reach it in
  `~/.code-visualizer/desktop.json` (a loopback URL and a token, removed when
  it quits); with no app running, the IDE opens the graph in the browser.

## Build

Needs uv, Rust and Node. One command rebuilds everything from the current
sources:

```bash
desktop/build.sh            # the cviz binary, the grammars if missing, the app
desktop/build.sh --app      # the app only (after a change to desktop/ alone)
desktop/build.sh --open     # then quit the running app and open the new one
```

By hand, the same steps:

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

The builds are not signed with an Apple or Microsoft certificate. On macOS
the app is signed ad hoc (`signingIdentity: "-"` in `tauri.conf.json`), so
the first launch says Apple cannot verify it: allow it once in System
Settings › Privacy & Security › *Open Anyway*. A build without that
signature is reported as "damaged"; `xattr -dr com.apple.quarantine "/Applications/Code Visualizer.app"` lets it open. The hardened
runtime is off: under it, the ad hoc signed `cviz` cannot load the Python
library it unpacks. Windows shows a SmartScreen warning (*More info* ›
*Run anyway*).
