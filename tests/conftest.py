from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_demo_builder():
    spec = importlib.util.spec_from_file_location("make_demo_db", ROOT / "examples" / "make_demo_db.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo_db(tmp_path: Path) -> Path:
    """A fresh copy of the synthetic mission-control database."""
    return _load_demo_builder().build(tmp_path / "mission_control.db")


@pytest.fixture
def tiny_db(tmp_path: Path) -> Path:
    """A small database with awkward names and values."""
    path = tmp_path / "tiny.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE "odd table" (id INTEGER PRIMARY KEY, "update" TEXT, note TEXT, payload BLOB);
        INSERT INTO "odd table" ("update", note, payload) VALUES
            ('drop table x', 'pipe | and' || char(10) || 'newline', x'00010203'),
            ('fine', NULL, NULL);
        CREATE TABLE numbers (n INTEGER);
        WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM seq WHERE x < 2500)
        INSERT INTO numbers SELECT x FROM seq;
        """
    )
    conn.commit()
    conn.close()
    return path
