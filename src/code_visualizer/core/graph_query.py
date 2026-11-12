# src/code_visualizer/core/graph_query.py
"""
ROADMAP M4 — GraphQueryInterface protocol for database backends.

CodeFinder and indexing pipelines should depend on this interface rather than
backend-specific Cypher dialects. Implementation: LadybugDB (Kùzu dialect,
core/database_embedded_kuzu.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple


class GraphQueryInterface(ABC):
    """Minimal contract shared by all CViz graph database managers."""

    name: str

    @abstractmethod
    def get_driver(self) -> Any:
        """Return a driver wrapper exposing ``session()``."""

    @abstractmethod
    def close_driver(self) -> None:
        """Release connections and embedded database resources."""

    @abstractmethod
    def is_connected(self) -> bool:
        """True when the backend is reachable and can execute a trivial query."""

    @abstractmethod
    def get_backend_type(self) -> str:
        """Stable backend identifier (``ladybugdb``)."""

    @classmethod
    @abstractmethod
    def validate_config(cls, *args, **kwargs) -> Tuple[bool, Optional[str]]:
        """Validate backend-specific configuration before connecting."""

    @classmethod
    @abstractmethod
    def test_connection(cls, *args, **kwargs) -> Tuple[bool, Optional[str]]:
        """Check whether the backend driver is installed and usable."""
