# Code Visualizer — VS Code extension (CodeGraphContext fork)

> **Fork notice.** This extension comes from
> [polysok/code-visualizer](https://github.com/polysok/code-visualizer), a fork of
> [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) (MIT).
> The original extension and engine are the work of the CodeGraphContext authors;
> this fork adds the IDE-linked dependency-graph page (`cviz graph`). It is not
> affiliated with the upstream project.
>
> Requires the fork's engine: `uv tool install git+https://github.com/polysok/code-visualizer.git`.

CodeGraphContext brings graph-native code intelligence into VS Code through CViz MCP.

## Highlights
- Command Center dashboard (index, watch, search, risk map, repo selector).
- Interactive call graph panel with editor synchronization.
- CodeLens, hover mini-map, dead-code diagnostics, and quick-fix action.
- Repositories, bundles, and watch state explorer views.
- Cypher console with direct graph querying.
- Variable impact radius and signature-change impact warnings.
- Engine configuration cockpit for executable/token controls (the graph is stored in LadybugDB, CViz's only backend).

## Build
- `npm install`
- `npm run compile`
- `npm run test`
- `npm run package:vsix`
# CodeGraphContext (CViz) VS Code Extension

The CViz extension brings powerful code graph intelligence directly into your VS Code workspace.

## Features

- **🚀 Indexing Wizard**: Create a code graph for your project with one click.
- **🔍 Editor Intelligence**: CodeLens and Hover metadata for complexity and call counts.
- **📊 Interactive Call Graph**: Visualize relationships in real-time.
- **🌐 Dependency graph in the browser**: `CViz: Open in Dependency Graph` (`Cmd/Ctrl+Alt+G`) opens the artifact under the cursor in the standalone graph page served by `cviz graph`; clicking a node there jumps back to the code in VS Code.
- **🛠️ CViz Explorer**: Manage Repositories, Bundles, and run Cypher queries.
- **🔗 Dead Code Diagnostics**: Find and fix unused code.

## Quick Start

1. Install the extension.
2. Open your project folder.
3. Run `CViz: Run Indexing Wizard` from the Command Palette (`Ctrl+Shift+P`).

Each repository has its own index in `<repo>/.code-visualizer/` (ignored by Git); the extension uses the one of the current file's repository.

## Configuration

Settings available under `CViz`:
- `cviz.executable`: Path to your `cviz` binary.
- `cviz.complexityWarningThreshold`: Set limit for complexity alerts.
- `cviz.graphView`: `web` (default) opens CodeLens/hover/context-menu call graphs in the browser page; `panel` keeps the in-editor webview.
- `cviz.graphServerUrl`: URL of a graph server to always use. Empty by default: each repository has its own server (`cviz graph --watch <repo>`, which records itself in `~/.code-visualizer/servers.json`); the extension finds the one of the current file's repository and offers to start it when it is not running.
