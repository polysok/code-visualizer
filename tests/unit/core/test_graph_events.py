"""Cross-process graph notifications (core/graph_events.py) and cviz-open parsing."""
from __future__ import annotations

import json
import time

import pytest

from code_visualizer.cli.open_in_graph import graph_url, parse_location
from code_visualizer.core import graph_events


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CVIZ_EVENTS_FILE", str(path))
    monkeypatch.delenv("CVIZ_EVENTS_DISABLED", raising=False)
    return path


def test_tail_delivers_only_new_events(events_file):
    graph_events.publish(graph_events.GRAPH_CHANGED, files=["/old.py"])
    tail = graph_events.EventTail()
    assert tail.poll() == []
    graph_events.publish(graph_events.GRAPH_CHANGED, files=["/r/b.py", "/r/a.py", "/r/a.py"], repo="/r")
    events = tail.poll()
    assert len(events) == 1
    assert events[0]["files"] == ["/r/a.py", "/r/b.py"]
    assert events[0]["repo"] == "/r"
    assert tail.poll() == []


def test_tail_survives_trim_without_duplicates(events_file, monkeypatch):
    tail = graph_events.EventTail()
    graph_events.publish(graph_events.GRAPH_CHANGED, files=["/1.py"])
    assert [e["files"] for e in tail.poll()] == [["/1.py"]]
    monkeypatch.setattr(graph_events, "MAX_BYTES", 10)
    monkeypatch.setattr(graph_events, "KEEP_LINES", 2)
    time.sleep(0.01)
    graph_events.publish(graph_events.INDEX_DONE, repo="/r")  # triggers a trim (new inode)
    events = tail.poll()
    assert [e["type"] for e in events] == [graph_events.INDEX_DONE]


def test_tail_ignores_partial_lines(events_file):
    events_file.write_text("")
    tail = graph_events.EventTail()
    line = json.dumps({"type": "graph:changed", "files": [], "ts": time.time() + 1})
    with open(events_file, "a") as fh:
        fh.write(line[:10])
    assert tail.poll() == []
    with open(events_file, "a") as fh:
        fh.write(line[10:] + "\n")
    assert [e["type"] for e in tail.poll()] == ["graph:changed"]


def test_publish_notifies_listeners_and_never_raises(events_file, monkeypatch, tmp_path):
    seen = []
    remove = graph_events.add_listener(seen.append)

    def boom(_event):
        raise RuntimeError("listener failure must not escape")

    remove_boom = graph_events.add_listener(boom)
    try:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        monkeypatch.setenv("CVIZ_EVENTS_FILE", str(blocker / "events.jsonl"))  # unwritable
        graph_events.publish(graph_events.GRAPH_CHANGED, files=["/x.py"])
    finally:
        remove()
        remove_boom()
    assert seen and seen[0]["files"] == ["/x.py"]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("src/a.py", ("src/a.py", None)),
        ("src/a.py:42", ("src/a.py", 42)),
        ("src/a.py:42:7", ("src/a.py", 42)),
        (r"C:\repo\Program.cs:12", (r"C:\repo\Program.cs", 12)),
        (r"C:\repo\Program.cs", (r"C:\repo\Program.cs", None)),
        ('"/with space/a.ts:3"', ("/with space/a.ts", 3)),
    ],
)
def test_parse_location(spec, expected):
    assert parse_location(spec) == expected


def test_graph_url_encodes_path():
    url = graph_url("http://localhost:8000/", "/a b/c#d.py", 3, depth=2, ide="rider")
    assert url.startswith("http://localhost:8000/graph?")
    assert "path=%2Fa+b%2Fc%23d.py" in url and "line=3" in url and "depth=2" in url and "ide=rider" in url
