from pathlib import Path

import pytest

from code_visualizer.core.database_ladybug import LadybugDBManager
from code_visualizer.tools.code_finder import CodeFinder
from code_visualizer.tools.indexing.persistence.writer import GraphWriter

kuzu = pytest.importorskip("ladybug")


def _fresh_kuzu_manager(db_path: Path) -> LadybugDBManager:
    if LadybugDBManager._instance is not None:
        LadybugDBManager._instance.close_driver()
    LadybugDBManager._instance = None
    LadybugDBManager._db = None
    LadybugDBManager._conn = None
    return LadybugDBManager(db_path=str(db_path))


class _KuzuDBAdapter:
    def __init__(self, driver):
        self._driver = driver

    def get_driver(self, graph_name=None):
        return self._driver

    def get_backend_type(self) -> str:
        return "ladybugdb"


def test_finds_functions_by_argument_name_or_type_without_duplicates(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    file_path = repo / "OrderRepository.java"
    file_path.write_text(
        "Page<Order> findAll(OrderFilter filter, Pageable pageable) {}\n",
        encoding="utf-8",
    )

    manager = _fresh_kuzu_manager(tmp_path / "db")
    driver = manager.get_driver()
    try:
        writer = GraphWriter(driver)
        writer.add_repository_to_graph(repo)
        writer.add_file_to_graph(
            {
                "path": str(file_path),
                "repo_path": str(repo),
                "lang": "java",
                "functions": [
                    {
                        "name": "findAll",
                        "line_number": 1,
                        "args": ["filter", "pageable"],
                        "arg_types": ["OrderFilter", "Pageable"],
                    }
                ],
                "classes": [],
                "variables": [],
            },
            repo.name,
            {},
            repo_path_str=str(repo.resolve()),
        )
        finder = CodeFinder(_KuzuDBAdapter(driver))

        assert [
            row["function_name"] for row in finder.find_functions_by_argument("filter")
        ] == ["findAll"]
        assert [
            row["function_name"]
            for row in finder.find_functions_by_argument("OrderFilter")
        ] == ["findAll"]
    finally:
        manager.close_driver()
