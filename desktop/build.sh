#!/bin/sh
# Rebuilds the desktop app locally, from the current sources:
#   1. the cviz binary (it serves the graph page: a change to the page needs it)
#   2. the tree-sitter grammars, if desktop/src-tauri/grammars/ is missing
#   3. the app (Tauri)
#
#   desktop/build.sh            # everything
#   desktop/build.sh --app      # the app only (after a change to desktop/ alone)
#   desktop/build.sh --open     # then quit the running app and open the new one
#
# Needs uv, Rust and Node. macOS: the result is
# desktop/src-tauri/target/release/bundle/macos/Code Visualizer.app
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DESKTOP="$ROOT/desktop"
TAURI="$DESKTOP/src-tauri"
APP_ONLY=0
OPEN=0
for arg in "$@"; do
    case "$arg" in
        --app) APP_ONLY=1 ;;
        --open) OPEN=1 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

step() { printf '\n== %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "$1 is needed: $2" >&2; exit 1; }; }
need uv "https://docs.astral.sh/uv/"
need cargo "https://rustup.rs"
need npm "https://nodejs.org"

TRIPLE=$(rustc -vV | sed -n 's/^host: //p')
case "$TRIPLE" in *windows*) EXE=.exe ;; *) EXE= ;; esac
SIDECAR="$TAURI/binaries/cviz-$TRIPLE$EXE"

cd "$ROOT"
if [ "$APP_ONLY" = 0 ] || [ ! -f "$SIDECAR" ]; then
    step "1/3 cviz binary (PyInstaller, a few minutes)"
    uv run python -c "import PyInstaller" 2>/dev/null || uv pip install --quiet pyinstaller
    uv run pyinstaller cviz.spec --clean --noconfirm --log-level WARN
    mkdir -p "$TAURI/binaries"
    cp "dist/cviz$EXE" "$SIDECAR"
    chmod +x "$SIDECAR"
else
    step "1/3 cviz binary: kept ($SIDECAR)"
fi

if [ ! -d "$TAURI/grammars" ]; then
    step "2/3 tree-sitter grammars"
    TREE_SITTER_LANGUAGE_PACK_CACHE_DIR="$TAURI/grammars" uv run python scripts/prefetch_grammars.py
else
    step "2/3 tree-sitter grammars: kept ($TAURI/grammars)"
fi

step "3/3 app (Tauri)"
cd "$DESKTOP"
[ -d node_modules ] || npm ci --no-audit --no-fund
case "$(uname -s)" in
    Darwin) BUNDLES=app ;;
    Linux) BUNDLES=deb ;;
    *) BUNDLES=nsis ;;
esac
npx tauri build --bundles "$BUNDLES"

APP="$TAURI/target/release/bundle/macos/Code Visualizer.app"
if [ "$BUNDLES" = app ]; then
    printf '\nBuilt: %s\n' "$APP"
    if [ "$OPEN" = 1 ]; then
        # Quitting stops the servers it started, which still run the old binary.
        osascript -e 'tell application "Code Visualizer" to quit' 2>/dev/null || true
        sleep 2
        open "$APP"
    else
        echo "Quit the running app (Cmd+Q) before opening the new one: its servers run the old binary."
    fi
else
    printf '\nBuilt: %s\n' "$TAURI/target/release/bundle/$BUNDLES"
fi
