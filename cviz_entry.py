# cviz_entry.py
# PyInstaller entrypoint — absolute imports only (no relative imports)
import sys
import os

# When frozen by PyInstaller, sys._MEIPASS is the temp extraction dir.
# We add it to the path so code_visualizer package is importable.
if getattr(sys, 'frozen', False):
    # Running as a PyInstaller bundle
    bundle_dir = sys._MEIPASS
    sys.path.insert(0, bundle_dir)

    # If the process is intended to be a FalkorDB worker (spawned by cviz itself)
    if os.getenv('CVIZ_RUN_FALKOR_WORKER') == 'true':
        from code_visualizer.core.falkor_worker import run_worker
        run_worker()
        sys.exit(0)

from code_visualizer.cli.main import app

if __name__ == '__main__':
    app()
