# cviz.spec
# Multi-platform PyInstaller build spec for Code Visualizer
# Supports: Linux (x86_64/Aarch64), Windows, macOS

import sys
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all

block_cipher = None

# ── Environment Detection ──────────────────────────────────────────────────
is_win = sys.platform == 'win32'
is_mac = sys.platform == 'darwin'
is_linux = sys.platform == 'linux' or sys.platform == 'linux2'

# Find site-packages dynamically using the 'site' module
import site
prefix = Path(sys.prefix)
search_paths = [prefix]
# Add standard site-packages locations
try:
    search_paths.extend([Path(p) for p in site.getsitepackages()])
except AttributeError:
    # Getsitepackages not available in some venv configs
    pass
# Add user-local site-packages
search_paths.append(Path(site.getusersitepackages()))
# Ensure we only have unique, existing paths
search_paths = list(set([p for p in search_paths if p.exists()]))


# ── 1. Component Lists (Binaries, Datas, Hidden Imports) ───────────────────
binaries = []
datas = []
hidden_imports = [
    'code_visualizer',
    'code_visualizer.cli',
    'code_visualizer.cli.main',
    'code_visualizer.cli.cli_helpers',
    'code_visualizer.cli.config_manager',
    'code_visualizer.cli.setup_wizard',
    'code_visualizer.cli.visualizer',
    'code_visualizer.core',
    'code_visualizer.core.database_embedded_kuzu',
    'code_visualizer.core.database_ladybug',
    'code_visualizer.core.jobs',
    'code_visualizer.core.watcher',
    'code_visualizer.core.cviz_bundle',
    'code_visualizer.server',
    'code_visualizer.tool_definitions',
    'code_visualizer.prompts',
    'code_visualizer.tools',
    'code_visualizer.tools.code_finder',
    'code_visualizer.tools.graph_builder',
    'code_visualizer.tools.package_resolver',
    'code_visualizer.tools.system',
    'code_visualizer.tools.scip_indexer',
    'code_visualizer.tools.scip_pb2',
    'code_visualizer.tools.advanced_language_query_tool',
    'code_visualizer.tools.languages',
    'code_visualizer.tools.languages.python',
    'code_visualizer.tools.languages.javascript',
    'code_visualizer.tools.languages.typescript',
    'code_visualizer.tools.languages.typescriptjsx',
    'code_visualizer.tools.languages.java',
    'code_visualizer.tools.languages.go',
    'code_visualizer.tools.languages.rust',
    'code_visualizer.tools.languages.c',
    'code_visualizer.tools.languages.cpp',
    'code_visualizer.tools.languages.ruby',
    'code_visualizer.tools.languages.php',
    'code_visualizer.tools.languages.csharp',
    'code_visualizer.tools.languages.kotlin',
    'code_visualizer.tools.languages.scala',
    'code_visualizer.tools.languages.swift',
    'code_visualizer.tools.languages.haskell',
    'code_visualizer.tools.languages.dart',
    'code_visualizer.tools.languages.perl',
    'code_visualizer.tools.query_tool_languages.python_toolkit',
    'code_visualizer.tools.query_tool_languages.javascript_toolkit',
    'code_visualizer.tools.query_tool_languages.typescript_toolkit',
    'code_visualizer.tools.query_tool_languages.java_toolkit',
    'code_visualizer.tools.query_tool_languages.go_toolkit',
    'code_visualizer.tools.query_tool_languages.rust_toolkit',
    'code_visualizer.tools.query_tool_languages.c_toolkit',
    'code_visualizer.tools.query_tool_languages.cpp_toolkit',
    'code_visualizer.tools.query_tool_languages.ruby_toolkit',
    'code_visualizer.tools.query_tool_languages.csharp_toolkit',
    'code_visualizer.tools.query_tool_languages.scala_toolkit',
    'code_visualizer.tools.query_tool_languages.swift_toolkit',
    'code_visualizer.tools.query_tool_languages.haskell_toolkit',
    'code_visualizer.tools.query_tool_languages.dart_toolkit',
    'code_visualizer.tools.query_tool_languages.perl_toolkit',
    'code_visualizer.tools.handlers.indexing_handlers',
    'code_visualizer.tools.handlers.management_handlers',
    'code_visualizer.tools.handlers.query_handlers',
    'code_visualizer.utils.debug_log',
    'code_visualizer.utils.tree_sitter_manager',
    'code_visualizer.utils.visualize_graph',

    'ladybug',
    'dotenv',
    'typer',
    'typer.core',
    'typer.main',
    'rich',
    'rich.console',
    'rich.table',
    'rich.progress',
    'rich.markup',
    'rich.panel',
    'tree_sitter',
    'tree_sitter_language_pack',
    'watchdog',
    'watchdog.observers',
    'watchdog.events',
    'anyio',
    'click',
    'shellingham',
    'httpx',
    'httpcore',
    'importlib',
    'asyncio',
    'threading',
    'subprocess',
    'socket',
    'atexit',
]


# Bin extensions by platform
ext = '*.so'
if is_win:
    ext = '*.pyd'
elif is_mac:
    ext = '*.dylib'

def find_pkg_dir(name):
    for p in sys.path:
        if not p: continue
        d = Path(p) / name
        if d.exists():
            return d
    return None

def add_binary(package_path, pattern, target_subdir=None):
    pkg_dir = find_pkg_dir(package_path)
    if pkg_dir:
        for f in pkg_dir.glob(pattern):
            if f.is_file():
                binaries.append((str(f), target_subdir or package_path))
    else:
        print(f"Warning: Could not find package directory: {package_path}")

# tree-sitter core
add_binary('tree_sitter', ext)

# Grammars: tree-sitter-language-pack downloads them on first use (the
# desktop app ships them, see desktop/README.md); C# comes as its own package.
add_binary('tree_sitter_c_sharp', ext)

# LadybugDB (the only graph backend): Python package plus its native engine
# (pybind extension and/or the lbug C-API shared library loaded via ctypes).
try:
    l_datas, l_binaries, l_hiddenimports = collect_all('ladybug')
    datas += l_datas
    binaries += l_binaries
    hidden_imports += l_hiddenimports
except Exception as e:
    print(f"WARNING: collect_all failed for ladybug: {e}")

# stdlibs: dynamically imports py3.py, py312.py, etc. via importlib
stdlibs_dir = find_pkg_dir('stdlibs')
if stdlibs_dir:
    for f in stdlibs_dir.glob('*.py'):
        datas.append((str(f), 'stdlibs'))

# mcp package data
datas += collect_data_files('mcp', includes=['**/*'])

# mcp.json shipped with CViz
mcp_json = Path('src/code_visualizer/mcp.json')
if mcp_json.exists():
    datas.append((str(mcp_json), 'code_visualizer'))

# Dependency-graph page (`cviz graph`): HTML/JS/CSS and the vendored D3,
# served from code_visualizer/viz/static at run time.
viz_static = Path('src/code_visualizer/viz/static')
if viz_static.is_dir():
    for f in viz_static.iterdir():
        if f.is_file():
            datas.append((str(f), 'code_visualizer/viz/static'))

# tree-sitter-language-pack: includes metadata needed at runtime
tslp_dir = find_pkg_dir('tree_sitter_language_pack')
if tslp_dir:
    datas += collect_data_files('tree_sitter_language_pack', includes=['**/*'])

# ── 3. Final Adjustments ────────────────────────────────────────────────────

# uvicorn picks its protocol/loop/lifespan implementations by name at run
# time; `cviz graph` / `cviz visualize` need them in the frozen binary.
hidden_imports += collect_submodules('uvicorn')
# Modules imported lazily (inside functions) by the graph page and the CLI.
hidden_imports += [
    'code_visualizer.viz.graph_api',
    'code_visualizer.viz.neighborhood',
    'code_visualizer.cli.open_in_graph',
    'code_visualizer.cli.ide_setup',
    'code_visualizer.core.graph_events',
    'code_visualizer.tools.languages.rust_types',
    'code_visualizer.tools.languages.member_types',
    'code_visualizer.utils.package_info',
    # Natural-language search (the binary ships no fastembed: it uses an
    # OpenAI-compatible embedding server).
    'code_visualizer.semantic',
    'code_visualizer.semantic.settings',
    'code_visualizer.semantic.embedders',
    'code_visualizer.semantic.indexer',
    'code_visualizer.semantic.search',
    'code_visualizer.semantic.chat',
    'code_visualizer.viz.ai_api',
]

# Add platform-specific watchers
if is_win:
    hidden_imports.append('watchdog.observers.read_directory_changes')
elif is_linux:
    hidden_imports.append('watchdog.observers.inotify')
    hidden_imports.append('watchdog.observers.inotify_buffer')
elif is_mac:
    hidden_imports.append('watchdog.observers.fsevents')

# ── 4. Analysis ──────────────────────────────────────────────────────────────
a = Analysis(
    ['cviz_entry.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', '_tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy',
        'PIL', 'cv2', 'torch', 'tensorflow', 'jupyter', 'notebook', 'IPython',
        'pydoc', 'doctest', 'xmlrpc', 'lib2to3', 'test', 'unittest.mock',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── 5. ONE-FILE EXE ──────────────────────────────────────────────────────────
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='cviz',
    debug=False,
    bootloader_ignore_signals=False,
    strip=not is_win,  # strip fails on windows often
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
