"""Coverage and real file-backed isolation must survive CI acceleration."""

from collections import Counter
import hashlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from backend.db.session import configure_database
from backend.tests.ci_support import assign_modules, restore_sqlite_database


@pytest.mark.parametrize("count", [1, 2, 4, 7])
def test_shards_cover_each_parametrized_test_once_and_keep_modules_together(count):
    nodeids = [f"tests/test_{module}.py::test_value[{case}]"
               for module, size in enumerate([17, 12, 8, 5, 2, 1]) for case in range(size)]
    assignments = assign_modules(nodeids, count)
    assert assignments == assign_modules(list(reversed(nodeids)), count)
    shards = [[node for node in nodeids if assignments[node.split("::")[0]] == index]
              for index in range(1, count + 1)]
    assert Counter(node for shard in shards for node in shard) == Counter(nodeids)
    for module in assignments:
        assert sum(any(node.startswith(f"{module}::") for node in shard) for shard in shards) == 1
    assert max(map(len, shards)) - min(map(len, shards)) <= 17


def test_shards_reject_invalid_count():
    with pytest.raises(ValueError):
        assign_modules(["test_example.py::test_value"], 0)


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_database_restore_removes_rows_and_schema_changes_without_losing_constraints(tmp_path, journal_mode):
    template = tmp_path / "template.db"
    source = create_engine(f"sqlite:///{template}")
    with source.begin() as connection:
        connection.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT UNIQUE)"))
        connection.execute(text("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"))
    source.dispose()
    original_hash = hashlib.sha256(template.read_bytes()).hexdigest()
    target = tmp_path / "test.db"
    engine = configure_database(f"sqlite:///{target}")
    try:
        restore_sqlite_database(engine, template, target)
        with engine.begin() as connection:
            connection.execute(text(f"PRAGMA journal_mode={journal_mode}"))
            connection.execute(text("INSERT INTO parent VALUES (1, 'previous-test')"))
            connection.execute(text("INSERT INTO child VALUES (1)"))
            connection.execute(text("ALTER TABLE parent ADD COLUMN leaked TEXT"))
            connection.execute(text("CREATE TABLE leaked_table (id INTEGER)"))

        restore_sqlite_database(engine, template, target)
        assert set(inspect(engine).get_table_names()) == {"parent", "child"}
        assert [col["name"] for col in inspect(engine).get_columns("parent")] == ["id", "name"]
        with engine.begin() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM parent")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM child")).scalar_one() == 0
            with pytest.raises(IntegrityError):
                connection.execute(text("INSERT INTO child VALUES (999)"))
            connection.execute(text("INSERT INTO parent VALUES (1, 'new-test')"))
            with pytest.raises(IntegrityError):
                connection.execute(text("INSERT INTO parent VALUES (2, 'new-test')"))
        assert hashlib.sha256(template.read_bytes()).hexdigest() == original_hash
    finally:
        engine.dispose()
