#!/bin/sh
# Installs CViz (macOS, Linux):
#
#   curl -LsSf https://github.com/polysok/code-visualizer/releases/latest/download/install.sh | sh
#
# Installs uv if it is missing (in your home, no admin rights; uv brings its
# own Python), then `cviz` with the local embedding model, then offers to open
# the graph of the Git repository you run it from. Run it again to upgrade.
#
# Environment:
#   CVIZ_VERSION   release to install (vX.Y.Z); default: the release this script comes from
#   CVIZ_WHEEL     wheel to install instead (URL or file path)
#   CVIZ_NO_START  1: install only, do not offer to open the graph
#   CVIZ_PYTHON    Python version for the tool (default 3.12)
set -eu

REPO_URL="https://github.com/polysok/code-visualizer"
RELEASE_TAG=""  # written by the release workflow
PYTHON="${CVIZ_PYTHON:-3.12}"

say() { printf '%s\n' "$*"; }
fail() { printf 'cviz install: %s\n' "$*" >&2; exit 1; }

download() {  # url -> stdout
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$1"
    elif command -v wget >/dev/null 2>&1; then wget -qO- "$1"
    else fail "neither curl nor wget is installed"
    fi
}

latest_tag() {  # the tag GitHub redirects /releases/latest to
    if command -v curl >/dev/null 2>&1; then
        curl -fsSLI -o /dev/null -w '%{url_effective}' "$REPO_URL/releases/latest"
    else
        wget -S --spider "$REPO_URL/releases/latest" 2>&1 | sed -n 's/^ *[Ll]ocation: *//p' | tail -n 1
    fi | sed 's#.*/tag/##' | tr -d '\r\n'
}

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
    for dir in "${XDG_BIN_HOME:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uv" ]; then PATH="$dir:$PATH"; break; fi
    done
fi
if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (https://docs.astral.sh/uv/)..."
    download https://astral.sh/uv/install.sh | sh
    PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv was installed but cannot be found; open a new terminal and run this again"
fi
export PATH

# 2. cviz
if [ -n "${CVIZ_WHEEL:-}" ]; then
    wheel="$CVIZ_WHEEL"
else
    tag="${CVIZ_VERSION:-$RELEASE_TAG}"
    [ -n "$tag" ] || tag="$(latest_tag)"
    case "$tag" in
        v[0-9]*) ;;
        [0-9]*) tag="v$tag" ;;
        *) fail "cannot find the latest release at $REPO_URL/releases (got '$tag'); set CVIZ_VERSION=vX.Y.Z" ;;
    esac
    wheel="$REPO_URL/releases/download/$tag/code_visualizer-${tag#v}-py3-none-any.whl"
fi

say "Installing cviz from $wheel ..."
uv tool install --force --python "$PYTHON" "code-visualizer[semantic] @ $wheel"

bin_dir="$(uv tool dir --bin)"
cviz="$bin_dir/cviz"
[ -x "$cviz" ] || fail "the install finished but $cviz is missing"
case ":$PATH:" in
    *":$bin_dir:"*) on_path=1 ;;
    *) on_path=0; uv tool update-shell >/dev/null 2>&1 || true ;;
esac
say ""
say "$("$cviz" version 2>&1 | tail -n 1) is installed."  # it prints on stderr
[ "$on_path" = 1 ] || say "Open a new terminal to have the 'cviz' command (it is in $bin_dir)."

# 3. The graph of the repository we are in
repo=""
if command -v git >/dev/null 2>&1; then
    repo="$(git rev-parse --show-toplevel 2>/dev/null || true)"
fi
if [ -z "$repo" ] || [ "${CVIZ_NO_START:-0}" = 1 ]; then
    say "Open the graph of a repository:  cd /path/to/repo && cviz graph --watch ."
    exit 0
fi

answer=n
# The script itself comes on stdin (curl | sh): ask on the terminal.
if [ -r /dev/tty ] && { exec 3</dev/tty; } 2>/dev/null; then
    printf 'Open the graph of %s now? It is indexed first if needed. [Y/n] ' "$repo" > /dev/tty
    read -r answer <&3 || answer=n
    exec 3<&-
    [ -n "$answer" ] || answer=y
fi
case "$answer" in
    [Yy]*)
        say "Starting the graph server (Ctrl+C stops it; 'cviz graph --watch .' starts it again)..."
        exec "$cviz" graph --watch "$repo"
        ;;
    *) say "Open it later with:  cd $repo && cviz graph --watch ." ;;
esac
