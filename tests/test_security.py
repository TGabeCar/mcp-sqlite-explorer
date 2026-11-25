"""Proof that the explorer cannot change the database, layer by layer."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from mcp_sqlite_explorer import db
from mcp_sqlite_explorer.db import QueryRejected


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


BLOCKED_SQL = [
    # plain writes
    "INSERT INTO spacecraft (name, class, crew_capacity, commissioned_on) VALUES ('X', 'probe', 0, '2030-01-01')",
    "UPDATE missions SET status = 'scrubbed'",
    "DELETE FROM telemetry_events",
    "REPLACE INTO spacecraft (spacecraft_id, name, class, crew_capacity, commissioned_on) VALUES (1, 'X', 'probe', 0, '2030-01-01')",
    # schema changes
    "DROP TABLE missions",
    "CREATE TABLE pwned (x)",
    "ALTER TABLE missions ADD COLUMN pwned TEXT",
    "CREATE TEMP VIEW v AS SELECT 1",
    # other files and housekeeping
    "ATTACH DATABASE 'other.db' AS other",
    "attach 'other.db' as other",
    "DETACH DATABASE main",
    "VACUUM",
    "VACUUM INTO 'copy.db'",
    "REINDEX",
    "ANALYZE",
    # transactions
    "BEGIN",
    "COMMIT",
    "SAVEPOINT s1",
    # dangerous or writing PRAGMAs
    "PRAGMA writable_schema = ON",
    "PRAGMA writable_schema=1",
    "PRAGMA query_only = OFF",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA user_version = 99",
    "PRAGMA database_list",
    "PRAGMA main.writable_schema",
    # multiple statements and hiding tricks
    "SELECT 1; DROP TABLE missions",
    "SELECT 1;DELETE FROM missions;",
    "SELECT 1 /* harmless */; UPDATE missions SET code = 'x'",
    "SELECT 1 -- comment\n; DELETE FROM missions",
    "WITH doomed AS (SELECT 1) DELETE FROM missions",
    "WITH x AS (SELECT 1) INSERT INTO spacecraft SELECT * FROM spacecraft",
    "EXPLAIN DELETE FROM missions",
    "/* only a comment */",
    "",
    "   ",
    "SELECT 'unterminated",
    "SELECT load_extension('evil')",
]


@pytest.mark.parametrize("sql", BLOCKED_SQL)
def test_blocked_statements_are_rejected_and_db_is_unchanged(demo_db: Path, sql: str) -> None:
    before = _digest(demo_db)
    with pytest.raises(QueryRejected):
        db.query(demo_db, sql)
    assert _digest(demo_db) == before


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'please DELETE me; DROP TABLE x' AS harmless",
        'SELECT "update" FROM "odd table"',
        "SELECT replace(note, 'pipe', 'PIPE') FROM \"odd table\"",
        "with t as (select 1 as x) select x from t;",
        "EXPLAIN QUERY PLAN SELECT * FROM numbers WHERE n = 5",
        "PRAGMA table_info(\"odd table\")",
        "PRAGMA main.index_list(numbers)",
        "PRAGMA user_version",
        "SELECT name FROM pragma_table_info('numbers')",
        "  -- leading comment\n  SELECT 1  ",
    ],
)
def test_read_only_statements_are_allowed(tiny_db: Path, sql: str) -> None:
    result = db.query(tiny_db, sql)
    assert result.startswith("|") or result.startswith("_")


def test_authorizer_blocks_writes_even_without_the_static_check(demo_db: Path) -> None:
    with db.connect_readonly(demo_db) as conn:
        for sql in (
            "DELETE FROM missions",
            "ATTACH DATABASE ':memory:' AS sneaky",
            "PRAGMA writable_schema = ON",
            "CREATE TEMP TABLE t (x)",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
                conn.execute(sql)


def test_query_only_pragma_blocks_writes_when_authorizer_is_removed(demo_db: Path) -> None:
    with db.connect_readonly(demo_db) as conn:
        conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)  # None only works on 3.11+
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM missions")


def test_read_only_file_mode_blocks_writes_when_every_other_layer_is_off(demo_db: Path) -> None:
    before = _digest(demo_db)
    with db.connect_readonly(demo_db) as conn:
        conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)  # None only works on 3.11+
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM missions")
            conn.commit()
    assert _digest(demo_db) == before


def test_runaway_query_is_stopped_by_the_timeout(tiny_db: Path) -> None:
    forever = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT max(x) FROM r"
    with pytest.raises(QueryRejected, match="longer than"):
        db.query(tiny_db, forever, timeout_seconds=0.2)


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    ghost = tmp_path / "does_not_exist.db"
    with pytest.raises(db.DatabaseNotFound):
        db.list_tables(ghost)
    assert not ghost.exists()


def test_table_names_cannot_inject_sql(demo_db: Path) -> None:
    with pytest.raises(QueryRejected, match="Unknown table"):
        db.sample_rows(demo_db, "missions; DROP TABLE missions")
    with pytest.raises(QueryRejected, match="Unknown table"):
        db.describe_table(demo_db, 'missions" --')
