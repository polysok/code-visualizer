from code_visualizer.core.database_ladybug import LadybugSessionWrapper

class _FakeRawResult:
    def get_column_names(self):
        return []

    def has_next(self):
        return False

    def get_next(self):
        return []

class _FakeConn:
    def __init__(self, fail_with=None):
        self.queries = []
        self.fail_with = fail_with

    def execute(self, query, params):
        self.queries.append((query, params))
        if self.fail_with is not None:
            raise Exception(self.fail_with)
        return _FakeRawResult()


def _make_importers_query():
    return """
    MATCH (file:File)-[imp:IMPORTS]->(module:Module {name: $module_name})
    WHERE 1=1
    OPTIONAL MATCH (repo:Repository)-[:CONTAINS]->(file)
    RETURN DISTINCT
        file.path as importer_file_path,
        imp.line_number as import_line_number,
        file.is_dependency as file_is_dependency,
        repo.name as repository_name
    ORDER BY file.is_dependency ASC, file.path
    LIMIT 50
    """


def _make_coimports_query():
    return """
    MATCH (file:File)-[:IMPORTS]->(target_module:Module {name: $module_name})
    MATCH (file)-[imp:IMPORTS]->(other_module:Module)
    WHERE other_module <> target_module
    RETURN DISTINCT
        other_module.name as imported_module,
        imp.alias as import_alias
    ORDER BY other_module.name
    LIMIT 50
    """


def test_rewrites_module_deps_query_for_kuzu_scope_and_ordering():
    conn = _FakeConn()
    session = LadybugSessionWrapper(conn)

    session.run(_make_importers_query(), module_name="json")

    translated = conn.queries[0][0]
    assert "WITH file, imp, module" in translated
    assert "OPTIONAL MATCH (repo:Repository)-[:" in translated
    assert "]->(file)" in translated
    assert "ORDER BY file_is_dependency ASC, importer_file_path" in translated


def test_rewrites_coimports_order_by_alias():
    conn = _FakeConn()
    session = LadybugSessionWrapper(conn)

    session.run(_make_coimports_query(), module_name="json")

    translated = conn.queries[0][0]
    assert "ORDER BY imported_module" in translated


def test_fail_fast_disables_query_type_after_first_known_compat_error():
    conn = _FakeConn(fail_with="Binder exception: Variable file is not in scope.")
    session = LadybugSessionWrapper(conn)

    # First run records a failed execution and triggers the guard.
    session.run(_make_importers_query(), module_name="json")
    assert len(conn.queries) == 1

    # Second run should be skipped without executing on the connection.
    session.run(_make_importers_query(), module_name="json")
    assert len(conn.queries) == 1


def test_sanitize_value_normalizes_list_of_dict_keys_for_unwind_batches():
    mixed_batch = [
        {"name": "json", "line_number": 1},
        {"name": "pathlib", "full_import_name": "pathlib", "line_number": None},
    ]

    normalized = LadybugSessionWrapper._sanitize_value(mixed_batch)

    assert isinstance(normalized, list)
    assert len(normalized) == 2
    assert set(normalized[0].keys()) == set(normalized[1].keys())
    assert "full_import_name" in normalized[0]
    assert normalized[0]["full_import_name"] is None
    assert normalized[1]["line_number"] == -1


def test_sanitize_value_deduplicates_identical_batch_rows():
    batch = [
        {"name": "$n", "line_number": 3, "path": "/repo/a.php"},
        {"name": "$n", "line_number": 3, "path": "/repo/a.php"},
    ]

    normalized = LadybugSessionWrapper._sanitize_value(batch)

    assert len(normalized) == 1


def test_import_batch_rewrite_sets_missing_full_import_name():
    conn = _FakeConn()
    session = LadybugSessionWrapper(conn)
    session._skip_unwind_fallback = True

    session.run(
        """
        UNWIND $batch AS row
        MATCH (f:File {path: $file_path})
        MERGE (m:Module {name: row.name})
        SET m.alias = row.alias,
            m.full_import_name = coalesce(row.full_import_name, m.full_import_name)
        MERGE (f)-[r:IMPORTS]->(m)
        SET r.line_number = row.line_number,
            r.alias = row.alias
        """,
        file_path="/repo/a.py",
        batch=[{"name": "json", "line_number": 1}, {"name": "pathlib", "alias": "pl"}],
    )

    translated, params = conn.queries[0]
    assert "m.alias = row.alias" not in translated
    assert all("full_import_name" in row for row in params["batch"])
    assert all("lang" in row for row in params["batch"])


def test_unwind_uid_injection_uses_fallback_for_missing_pk_fields():
    conn = _FakeConn()
    session = LadybugSessionWrapper(conn)

    session.run(
        """
        UNWIND $batch AS row
        MERGE (n:Function {name: row.name, path: $file_path, line_number: row.line_number})
        SET n += row
        """,
        file_path="/repo/a.py",
        batch=[
            {"name": "$n", "line_number": None, "source": "x"},
            {"name": "$n", "line_number": None, "source": "y"},
        ],
    )

    translated, params = conn.queries[0]
    assert "uid: row.uid" in translated
    assert "MERGE (n:Function {uid: row.uid})" in translated
    assert all("uid" in row for row in params["batch"])
    assert params["batch"][0]["uid"] != params["batch"][1]["uid"]


def test_inheritance_queries_bypass_fail_fast_guard():
    session = LadybugSessionWrapper(_FakeConn())
    q = "MATCH (a)-[:INHERITS]->(b) MERGE (a)-[:INHERITS]->(b)"
    assert session._classify_query_type(q) == "inheritance_resolution"
    assert not session._should_fail_fast(
        "inheritance_resolution",
        Exception("Binder exception: Create rel  bound by multiple node labels is not supported."),
    )
