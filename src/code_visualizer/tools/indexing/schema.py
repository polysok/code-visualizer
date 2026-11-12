# src/code_visualizer/tools/indexing/schema.py
"""Graph schema hook of the indexing pipeline.

LadybugDB (the only backend) declares its node and relationship tables and keys
itself when the database is opened
(``core/database_embedded_kuzu.py``); Cypher ``CREATE INDEX`` / ``CREATE
CONSTRAINT`` statements are not part of its dialect. Nothing is left to do
here; the function is kept as the pipeline's schema step.
"""

from typing import Any

from ...utils.debug_log import info_logger


def create_graph_schema(driver: Any, db_manager: Any) -> None:
    """Schema step of the pipeline (LadybugDB creates its tables on open)."""
    info_logger("Database schema managed by LadybugDB (tables created on open)")
