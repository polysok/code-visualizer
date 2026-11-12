"""Utility functions for database operations and backend detection (LadybugDB)."""

from typing import Any


def get_backend_type(driver: Any, db_manager: Any = None) -> str:
    """Backend id reported by the manager or the driver (``ladybugdb``)."""
    if db_manager is not None and hasattr(db_manager, "get_backend_type"):
        return getattr(db_manager, "get_backend_type", None)()
    return getattr(driver, "get_backend_type", lambda: "ladybugdb")()


def execute_write_operation(driver: Any, backend: str, work_fn: callable) -> Any:
    """Run ``work_fn(session)`` for a write (``backend`` kept for the callers)."""
    with driver.session() as session:
        return work_fn(session)


def execute_read_operation(driver: Any, backend: str, work_fn: callable) -> Any:
    """Run ``work_fn(session)`` for a read (``backend`` kept for the callers)."""
    with driver.session() as session:
        return work_fn(session)
