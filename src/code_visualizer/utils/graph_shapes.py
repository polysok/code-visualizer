"""Classification of values returned by the LadybugDB driver wrapper.

Code Visualizer renders the graph through two independent paths — the offline
renderer in ``cli/cli_helpers.py`` and the live web server in ``viz/server.py``
— and both share the predicates below so they cannot drift apart.

LadybugDB hands back plain dicts. Nodes carry ``_ID`` / ``_LABEL``;
relationships carry ``_SRC`` / ``_DST`` / ``_LABEL`` (and their own ``_ID``).
Older Kùzu-style results spelled the same fields in lower case, so every lookup
accepts either casing.
"""

from typing import Any

__all__ = ["has_meta", "meta", "is_node", "is_relationship", "public_properties", "SEARCH_PROPERTY_PREFIX"]

#: Search data stored on artifact nodes (semantic/graph_index): keyword text,
#: hash and vectors. Internal, and the vectors are large: never shown.
SEARCH_PROPERTY_PREFIX = "search_"


def has_meta(record: dict, key: str) -> bool:
    """True if ``record`` carries the internal field ``key`` in either casing."""
    return key in record or key.upper() in record


def meta(record: dict, key: str, default: Any = None) -> Any:
    """Read an internal ``_``-prefixed field regardless of its casing."""
    if key in record:
        return record[key]
    return record.get(key.upper(), default)


def public_properties(record: dict) -> dict:
    """Node properties without internal fields (``_ID``…) and search data."""
    return {
        k: v for k, v in record.items()
        if not k.startswith("_") and not k.startswith(SEARCH_PROPERTY_PREFIX)
    }


def is_relationship(value: Any) -> bool:
    """True if ``value`` is a relationship dict (it carries both endpoints)."""
    return isinstance(value, dict) and has_meta(value, "_src") and has_meta(value, "_dst")


def is_node(value: Any) -> bool:
    """True if ``value`` is a node dict.

    A half-formed relationship — ``_src`` present but ``_dst`` missing — is not
    a usable relationship, but it also carries ``_label``, so a plain label
    check would turn it into a phantom node with a relationship's id. Values
    like that are classified as neither and skipped.
    """
    if not isinstance(value, dict):
        return False
    if has_meta(value, "_src") or has_meta(value, "_dst"):
        return False
    return has_meta(value, "_label") or "labels" in value
