"""Node / relationship classification shared by both graph renderers.

Code Visualizer renders the same graph twice — offline via
``cli/cli_helpers.py`` and live via ``viz/server.py`` — and both classify the
values the driver returns through ``utils/graph_shapes``.

LadybugDB returns plain dicts (``MATCH (n)-[e]->(m) RETURN n, e, m``):

    >>> list(node.keys()); list(rel.keys())
    ['_ID', '_LABEL', 'name', 'path']
    ['_SRC', '_DST', '_LABEL', '_ID']

Older Kùzu-style results used the same fields in lower case; both are accepted.
"""
import pytest

from code_visualizer.utils import graph_shapes
from code_visualizer.viz import server


KUZU_NODE = {"_id": "0:1", "_label": "File", "path": "/repo/a.py", "name": "a.py"}
KUZU_REL = {"_src": "0:1", "_dst": "0:2", "_label": "CONTAINS", "_id": "1:0"}
LADYBUG_NODE = {"_ID": "0:1", "_LABEL": "File", "path": "/repo/a.py", "name": "a.py"}
LADYBUG_REL = {"_SRC": "0:1", "_DST": "0:2", "_LABEL": "CONTAINS", "_ID": "1:0"}


# --- classification -----------------------------------------------------------

@pytest.mark.parametrize("value", [
    pytest.param(KUZU_NODE, id="kuzu"),
    pytest.param(LADYBUG_NODE, id="ladybug"),
])
def test_nodes_classify_as_nodes(value):
    assert graph_shapes.is_node(value) is True
    assert graph_shapes.is_relationship(value) is False


@pytest.mark.parametrize("value", [
    pytest.param(KUZU_REL, id="kuzu"),
    pytest.param(LADYBUG_REL, id="ladybug"),
])
def test_relationships_classify_as_relationships(value):
    assert graph_shapes.is_relationship(value) is True
    assert graph_shapes.is_node(value) is False


def test_half_formed_relationship_is_neither():
    """A relationship dict missing an endpoint must not become a phantom node.

    It still carries ``_label``, so a bare label check would admit it as a node
    whose id is a relationship id. Classifying it as neither drops it, which is
    what the offline renderer has always done.
    """
    half = {"_src": "0:1", "_label": "CONTAINS"}
    assert graph_shapes.is_relationship(half) is False
    assert graph_shapes.is_node(half) is False


def test_meta_reads_either_casing():
    assert graph_shapes.meta(KUZU_NODE, "_label") == "File"
    assert graph_shapes.meta(LADYBUG_NODE, "_label") == "File"
    assert graph_shapes.meta({}, "_label", "fallback") == "fallback"
    assert graph_shapes.has_meta(LADYBUG_REL, "_src") is True
    assert graph_shapes.has_meta({}, "_src") is False


# --- live server -------------------------------------------------------------

def test_server_renders_ladybug_nodes_and_edges():
    nodes, edges = {}, []
    a = {"_ID": {"table": 0, "offset": 1}, "_LABEL": "File", "name": "a.py", "path": "/repo/a.py"}
    b = {"_ID": {"table": 1, "offset": 2}, "_LABEL": "Function", "name": "alpha", "path": "/repo/a.py"}
    rel = {"_SRC": a["_ID"], "_DST": b["_ID"], "_LABEL": "CONTAINS", "_ID": {"table": 5, "offset": 0}}
    for value in (a, rel, b):
        server.parse_element(value, nodes, edges)

    assert sorted(nodes) == ["0_1", "1_2"]
    assert nodes["0_1"]["type"] == "File"
    assert edges == [{"id": "5_0", "source": "0_1", "target": "1_2", "type": "CONTAINS"}]
