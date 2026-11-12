"""Entry points of the graph page: functions nothing calls, that call the most."""
import asyncio

import pytest


@pytest.mark.integration
def test_entry_points_rank_main_first_and_skip_tests_and_vendored_code(tmp_path):
    pytest.importorskip("ladybug")
    from code_visualizer.core import ladybugdb_unavailable_reason

    if ladybugdb_unavailable_reason():
        pytest.skip("LadybugDB engine unavailable")
    from code_visualizer.core.database_ladybug import LadybugDBManager
    from code_visualizer.core.jobs import JobManager
    from code_visualizer.tools.graph_builder import GraphBuilder
    from code_visualizer.viz import neighborhood as nb

    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "lib.py").write_text(
        "def a():\n    return 1\n\ndef b():\n    return 2\n\ndef c():\n    return 3\n", encoding="utf-8")
    (repo / "main.py").write_text(
        "from lib import a\n\ndef main():\n    return a()\n", encoding="utf-8")
    (repo / "jobs.py").write_text(
        "from lib import a, b, c\n\ndef nightly():\n    a()\n    b()\n    return c()\n\n"
        "def helper():\n    return nightly()\n", encoding="utf-8")
    (repo / "tests" / "test_lib.py").write_text(
        "from lib import a, b, c\n\ndef test_all():\n    a()\n    b()\n    c()\n    a()\n", encoding="utf-8")

    manager = LadybugDBManager(str(tmp_path / "db"))
    try:
        jobs = JobManager()

        async def index():
            builder = GraphBuilder(manager, jobs, asyncio.get_running_loop())
            await builder.build_graph_from_path_async(repo, job_id=jobs.create_job(str(repo)))

        asyncio.run(index())
        with manager.get_driver().session() as session:
            names = [e["name"] for e in nb.entry_points(session, repo.resolve().as_posix() + "/", 6)]
    finally:
        manager.close_driver()

    assert names[0] == "main"                 # conventional name first
    assert "helper" in names                  # called by nobody, calls nightly
    assert "nightly" not in names             # called by helper: not an entry point
    assert "test_all" not in names            # test code is skipped
    assert not {"a", "b", "c"} & set(names)   # call nothing
