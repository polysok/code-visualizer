"""A WAL holding search-index writes, left by a crash, must not brick the database.

LadybugDB replays the WAL before any extension can be loaded, so a WAL with
fts / vector index records cannot be replayed (error or segfault). The
manager sets it aside and opens from the last checkpoint.
"""
import subprocess
import sys

import pytest

CRASH = r'''
import os, sys, ladybug as lb
db = lb.Database(sys.argv[1]); c = lb.Connection(db)
for q in ["LOAD fts", "CREATE NODE TABLE F(uid STRING, t STRING, PRIMARY KEY(uid))",
          "CREATE (:F {uid: 'a', t: 'hello'})", "CHECKPOINT",
          "CALL CREATE_FTS_INDEX('F', 'fi', ['t'])", "CREATE (:F {uid: 'b', t: 'lost'})"]:
    c.execute(q)
os._exit(0)  # killed: no checkpoint, the WAL keeps the index writes
'''

OPEN = r'''
import sys
from code_visualizer.core.database_ladybug import LadybugDBManager
m = LadybugDBManager(sys.argv[1])
with m.get_driver().session() as s:
    print("rows", s.run_native("MATCH (n:F) RETURN count(n) AS c").data()[0]["c"])
m.close_driver()
'''


@pytest.mark.integration
def test_unreplayable_wal_is_set_aside(tmp_path):
    pytest.importorskip("ladybug")
    from code_visualizer.core import ladybugdb_unavailable_reason

    if ladybugdb_unavailable_reason():
        pytest.skip("LadybugDB engine unavailable")
    db = tmp_path / "db"
    crash = subprocess.run([sys.executable, "-c", CRASH, str(db)], capture_output=True, text=True)
    if crash.returncode != 0:
        pytest.skip(f"fts extension unavailable: {crash.stderr[-300:]}")
    assert (tmp_path / "db.wal").exists()

    opened = subprocess.run([sys.executable, "-c", OPEN, str(db)], capture_output=True, text=True, timeout=300)
    assert opened.returncode == 0, opened.stderr[-3000:]
    assert "rows 1" in opened.stdout  # back to the last checkpoint
    assert "could not be replayed" in opened.stderr
    assert not (tmp_path / "db.wal").exists()
    assert list(tmp_path.glob("db.wal.unreplayable-*"))
