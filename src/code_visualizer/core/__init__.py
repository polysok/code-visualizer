# src/code_visualizer/core/__init__.py
"""
Core database management module.

Code Visualizer stores the code graph in LadybugDB (embedded, single process),
the only supported backend. Its native ``vector`` extension backs the
natural-language search of the graph page (keywords: BM25, semantic/graph_index).
"""
import functools
import importlib
import importlib.util
import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .database_ladybug import LadybugDBManager

SUPPORTED_BACKEND = "ladybugdb"


@functools.lru_cache(maxsize=1)
def ladybugdb_unavailable_reason() -> Optional[str]:
    """Return None when LadybugDB can actually run here, else why it cannot.

    An installed ``ladybug`` package is not enough: its engine is either a
    bundled pybind extension or the ``lbug`` C-API shared library loaded via
    ctypes, and some wheels (Windows, #1731) ship neither. Probing only the
    package made such an interpreter look ready, so explicit selection
    picked a backend that died on first use with
    "Could not find lbug C API shared library".
    """
    try:
        if importlib.util.find_spec("ladybug") is None:
            return "LadybugDB is not installed"
    except (ImportError, ValueError):
        return "LadybugDB is not installed"
    try:
        from ladybug._backend import get_capi_module, get_pybind_module
    except ImportError:
        # Package layout without the dual-backend loader: importing it is the
        # best available signal.
        try:
            import ladybug  # noqa: F401
        except Exception as e:
            return f"LadybugDB failed to import: {e}"
        return None
    try:
        if get_pybind_module() is not None:
            return None
    except Exception:
        pass
    # ladybug swallows the extension's ImportError before falling back to the
    # C-API library, so the fallback's "Could not find lbug C API shared
    # library" hides the real cause (e.g. a DLL load failure). Re-import once
    # to recover it for the message.
    try:
        importlib.import_module("ladybug._lbug")
        pybind_error = None
    except Exception as e:
        pybind_error = f"{type(e).__name__}: {e}"
    try:
        get_capi_module()
    except Exception as e:
        detail = f"C-API: {e}"
        if pybind_error:
            detail = f"extension: {pybind_error}; {detail}"
        return f"LadybugDB is installed but its native engine could not be loaded ({detail})"
    return None


def _is_ladybugdb_available() -> bool:
    """Check if LadybugDB is installed and its native engine loads."""
    return ladybugdb_unavailable_reason() is None


def get_database_manager(db_path: Optional[str] = None) -> "LadybugDBManager":
    """Return the LadybugDB manager (the only backend).

    ``CVIZ_RUNTIME_DB_TYPE`` / ``DEFAULT_DATABASE`` are still read so that a
    configuration naming a removed backend fails with a clear message instead
    of silently opening an empty database.
    """
    from code_visualizer.utils.debug_log import info_logger

    requested = (os.getenv("CVIZ_RUNTIME_DB_TYPE") or os.getenv("DEFAULT_DATABASE") or SUPPORTED_BACKEND).lower()
    if requested != SUPPORTED_BACKEND:
        raise ValueError(
            f"Database backend '{requested}' is not supported: Code Visualizer only uses LadybugDB.\n"
            "Remove DEFAULT_DATABASE / CVIZ_RUNTIME_DB_TYPE (or set them to 'ladybugdb') and re-index."
        )
    reason = ladybugdb_unavailable_reason()
    if reason:
        raise ValueError(f"{reason}.\nRun 'pip install ladybug'.")
    from .database_ladybug import LadybugDBManager

    info_logger(f"Using LadybugDB at {db_path or 'default path'}")
    return LadybugDBManager(db_path=db_path)


def __getattr__(name: str):
    if name == "LadybugDBManager":
        from .database_ladybug import LadybugDBManager

        return LadybugDBManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["LadybugDBManager", "get_database_manager", "ladybugdb_unavailable_reason", "SUPPORTED_BACKEND"]
