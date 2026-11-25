"""Behaviour of the plain tool functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_sqlite_explorer import db
from mcp_sqlite_explorer.db import QueryRejected


def test_list_tables_shows_tables_views_and_counts(demo_db: Path) -> None:
    out = db.list_tables(demo_db)
    assert out.splitlines()[0] == "| name | type | rows |"
    assert "| spacecraft | table | 6 |" in out
    assert "| crew_members | table | 20 |" in out
    assert "| mission_summary | view | 24 |" in out


def test_describe_table_reports_columns_indexes_and_foreign_keys(demo_db: Path) -> None:
    out = db.describe_table(demo_db, "missions")
    assert "## missions (table, 24 rows)" in out
    assert "| mission_id | INTEGER | no |  | PK |  |" in out
    assert "| spacecraft_id | INTEGER | no |  |  | spacecraft.spacecraft_id |" in out
    assert "idx_missions_status" in out
    assert "| spacecraft_id | spacecraft.spacecraft_id | RESTRICT | NO ACTION |" in out


def test_describe_table_is_case_insensitive_and_handles_composite_keys(demo_db: Path) -> None:
    out = db.describe_table(demo_db, "MISSION_CREW")
    assert "## mission_crew" in out
    assert "PK1" in out and "PK2" in out


def test_describe_unknown_table_lists_known_ones(demo_db: Path) -> None:
    with pytest.raises(QueryRejected, match="Known: .*spacecraft"):
        db.describe_table(demo_db, "aliens")


def test_query_returns_markdown_table(demo_db: Path) -> None:
    out = db.query(demo_db, "SELECT name, crew_capacity FROM spacecraft ORDER BY spacecraft_id LIMIT 2")
    lines = out.splitlines()
    assert lines[0] == "| name | crew_capacity |"
    assert lines[1] == "|---|---|"
    assert lines[2] == "| Halcyon Drift | 6 |"
    assert out.endswith("_2 rows._")


def test_query_caps_rows_and_says_so(tiny_db: Path) -> None:
    out = db.query(tiny_db, "SELECT n FROM numbers", limit=10)
    assert len([line for line in out.splitlines() if line.startswith("| ") and line != "| n |"]) == 10
    assert "more are available" in out


def test_query_limit_is_clamped_to_the_hard_maximum(tiny_db: Path) -> None:
    out = db.query(tiny_db, "SELECT n FROM numbers", limit=1_000_000)
    body = [line for line in out.splitlines() if line.startswith("| ") and line != "| n |"]
    assert len(body) == db.MAX_ROWS


def test_cells_are_truncated_escaped_and_typed(tiny_db: Path) -> None:
    out = db.query(tiny_db, "SELECT note, payload, printf('%.500c', 'x') AS long FROM \"odd table\" ORDER BY id")
    first = out.splitlines()[2]
    assert "pipe \\| and newline" in first  # pipe escaped, newline flattened
    assert "<blob 4 bytes>" in first
    assert "x" * (db.MAX_CELL_CHARS - 1) + "…" in first
    assert "x" * db.MAX_CELL_CHARS not in first
    assert "| NULL | NULL |" in out.splitlines()[3]


def test_sample_rows_defaults_and_cap(demo_db: Path) -> None:
    out = db.sample_rows(demo_db, "spacecraft")
    assert out.endswith("_5 rows._") is False  # 6 rows exist, so 5 are shown and more flagged
    assert "Showing the first 5 rows" in out
    big = db.sample_rows(demo_db, "telemetry_events", n=500)
    assert f"Showing the first {db.MAX_SAMPLE_ROWS} rows" in big


def test_sample_rows_works_on_views(demo_db: Path) -> None:
    out = db.sample_rows(demo_db, "mission_summary", n=2)
    assert out.splitlines()[0] == "| code | spacecraft | destination | status | crew | events |"


def test_schema_diagram_is_mermaid_with_relationships(demo_db: Path) -> None:
    out = db.schema_diagram(demo_db)
    assert out.startswith("erDiagram")
    assert "    missions {" in out
    assert "INTEGER mission_id PK,FK" in out  # mission_crew composite key
    assert 'spacecraft ||--o{ missions : "spacecraft_id"' in out
    assert 'missions ||--o{ telemetry_events : "mission_id"' in out
    assert "mission_summary" not in out  # views are not entities


def test_schema_diagram_sanitizes_awkward_names(tiny_db: Path) -> None:
    out = db.schema_diagram(tiny_db)
    assert "odd_table {" in out
    assert "TEXT update" in out


def test_schema_overview_includes_create_statements(demo_db: Path) -> None:
    out = db.schema_overview(demo_db)
    assert "## telemetry_events (table)" in out
    assert "CREATE VIEW mission_summary" in out


def test_shipped_demo_db_matches_the_script(demo_db: Path) -> None:
    shipped = Path(__file__).resolve().parents[1] / "examples" / "mission_control.db"
    assert shipped.is_file(), "run: python examples/make_demo_db.py"
    assert shipped.stat().st_size < 200_000
    assert db.list_tables(shipped) == db.list_tables(demo_db)
    assert db.schema_diagram(shipped) == db.schema_diagram(demo_db)
