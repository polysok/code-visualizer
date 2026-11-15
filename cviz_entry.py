# cviz_entry.py
# PyInstaller entrypoint — absolute imports only (no relative imports)
import sys

# When frozen by PyInstaller, sys._MEIPASS is the temp extraction dir.
# We add it to the path so code_visualizer package is importable.
if getattr(sys, 'frozen', False):
    sys.path.insert(0, sys._MEIPASS)

    # cviz relaunches itself through sys.executable the way it runs a Python
    # interpreter: `-m code_visualizer.cli.main graph ...` (the graph server
    # the MCP server starts), `-c <code>` (the WAL probe of the database).
    # In the frozen binary sys.executable is this program: behave like one.
    if len(sys.argv) > 2 and sys.argv[1] == '-c':
        code = sys.argv[2]
        sys.argv = ['-c'] + sys.argv[3:]
        exec(compile(code, '<string>', 'exec'), {'__name__': '__main__'})
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == '-m':
        import runpy
        module = sys.argv[2]
        sys.argv = [sys.executable] + sys.argv[3:]
        runpy.run_module(module, run_name='__main__', alter_sys=True)
        sys.exit(0)

from code_visualizer.cli.main import app

if __name__ == '__main__':
    app()
