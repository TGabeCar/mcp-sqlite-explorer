"""Read-only SQLite exploration helpers.

Everything in here is a plain function that takes a database path, so it can be
tested (and reused) without an MCP client. ``server.py`` wires these functions
to MCP tools.

Read-only is enforced in four independent layers:

1. The connection is opened with a ``mode=ro`` URI, so SQLite itself refuses
   to write to the file.
2. ``PRAGMA query_only = ON`` is set on every connection.
3. An authorizer callback (``sqlite3.Connection.set_authorizer``) denies every
   action that is not a read: writes, ``ATTACH``/``DETACH``, transactions,
   schema changes, ``load_extension`` and any PRAGMA outside a read-only
   allowlist.
4. :func:`validate_readonly_sql` statically rejects anything that is not a
   single ``SELECT`` / ``WITH`` / ``EXPLAIN`` / read-only ``PRAGMA`` statement
   before it ever reaches SQLite.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #

DEFAULT_LIMIT = 100
MAX_ROWS = 1000
MAX_SAMPLE_ROWS = 50
MAX_CELL_CHARS = 200
QUERY_TIMEOUT_SECONDS = 5.0
MAX_SQL_CHARS = 20_000

# PRAGMAs that only read. Ones in the first set take an argument such as a
# table name (``PRAGMA table_info(orders)``); ones in the second set must be
# used bare (``PRAGMA user_version``), never with ``=``.
PRAGMAS_WITH_ARG = frozenset(
    {
        "table_info",
        "table_xinfo",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
        "foreign_key_check",
        "integrity_check",
        "quick_check",
    }
)
PRAGMAS_NO_ARG = frozenset(
    {
        "table_list",
        "collation_list",
        "function_list",
        "compile_options",
        "encoding",
        "page_count",
        "page_size",
        "freelist_count",
        "user_version",
        "application_id",
        "schema_version",
        "foreign_keys",
    }
)
READ_ONLY_PRAGMAS = PRAGMAS_WITH_ARG | PRAGMAS_NO_ARG

ALLOWED_FIRST_KEYWORDS = frozenset({"SELECT", "WITH", "EXPLAIN", "PRAGMA"})

# Keywords that never belong in a read-only query. They are matched on SQL with
# string literals, quoted identifiers and comments removed, so a value like
# 'please delete me' or a column called "update" does not trip them.
FORBIDDEN_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE",
        "DROP",
        "ALTER",
        "ATTACH",
        "DETACH",
        "VACUUM",
        "REINDEX",
        "ANALYZE",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
    }
)

DENIED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})


class QueryRejected(ValueError):
    """Raised when SQL is refused by the read-only guard."""


class DatabaseNotFound(FileNotFoundError):
    """Raised when the configured database file does not exist."""


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


def resolve_db_path(path: str | os.PathLike[str]) -> Path:
    """Return the absolute path of an existing SQLite file or raise."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise DatabaseNotFound(f"SQLite database not found: {resolved}")
    return resolved


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, getattr(sqlite3, "SQLITE_RECURSIVE", 33)):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        name = (arg2 or "").lower()
        return sqlite3.SQLITE_DENY if name in DENIED_FUNCTIONS else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in PRAGMAS_WITH_ARG:
            return sqlite3.SQLITE_OK
        if name in PRAGMAS_NO_ARG and arg2 is None:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    # INSERT, UPDATE, DELETE, CREATE_*, DROP_*, ALTER_TABLE, ATTACH, DETACH,
    # TRANSACTION, SAVEPOINT, ANALYZE, REINDEX, ... are all refused.
    return sqlite3.SQLITE_DENY


@contextmanager
def connect_readonly(
    path: str | os.PathLike[str],
    timeout_seconds: float = QUERY_TIMEOUT_SECONDS,
) -> Iterator[sqlite3.Connection]:
    """Open ``path`` read-only with every guard layer switched on."""
    db_file = resolve_db_path(path)
    uri = f"{db_file.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + timeout_seconds
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Static SQL guard
# --------------------------------------------------------------------------- #


def _strip_sql(sql: str) -> str:
    """Blank out comments, string literals and quoted identifiers.

    The result keeps the statement's shape (keywords, punctuation) so it can be
    checked, but cannot be fooled by text hidden inside quotes or comments.
    Raises :class:`QueryRejected` on an unterminated quote or comment.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    closers = {"'": "'", '"': '"', "`": "`", "[": "]"}
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise QueryRejected("Unterminated /* comment.")
            i = end + 2
            out.append(" ")
        elif ch in closers:
            close = closers[ch]
            j = i + 1
            while True:
                j = sql.find(close, j)
                if j == -1:
                    raise QueryRejected(f"Unterminated quoted text starting with {ch}.")
                # SQL escapes a quote by doubling it: 'it''s'
                if close != "]" and sql.startswith(close * 2, j):
                    j += 2
                    continue
                break
            out.append("''" if ch == "'" else '""')
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


_PRAGMA_RE = re.compile(
    r"^PRAGMA\s+(?:[A-Za-z_]\w*\s*\.\s*)?(?P<name>[A-Za-z_]\w*)\s*(?P<arg>\(\s*[^()]*\))?$",
    re.IGNORECASE,
)


def validate_readonly_sql(sql: str) -> str:
    """Return the trimmed SQL if it is a single read-only statement.

    Raises :class:`QueryRejected` with a human-readable reason otherwise.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise QueryRejected("Empty query.")
    if len(sql) > MAX_SQL_CHARS:
        raise QueryRejected(f"Query is longer than {MAX_SQL_CHARS} characters.")

    shape = _strip_sql(sql).strip()
    shape = re.sub(r"[;\s]+$", "", shape)
    if not shape:
        raise QueryRejected("Query contains only comments.")
    if ";" in shape:
        raise QueryRejected("Only a single statement is allowed (found ';' between statements).")

    first = shape.split(None, 1)[0].upper()
    if first not in ALLOWED_FIRST_KEYWORDS:
        raise QueryRejected(
            f"Only SELECT, WITH, EXPLAIN and read-only PRAGMA statements are allowed (got {first})."
        )

    words = {w.upper() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", shape)}
    hits = sorted(words & FORBIDDEN_KEYWORDS)
    if hits:
        raise QueryRejected(f"Write or admin keyword not allowed: {', '.join(hits)}.")
    # REPLACE is fine as the string function replace(...), not as REPLACE INTO.
    if re.search(r"\bREPLACE\b(?!\s*\()", shape, re.IGNORECASE):
        raise QueryRejected("Write keyword not allowed: REPLACE.")

    if first == "PRAGMA":
        if "=" in shape:
            raise QueryRejected("PRAGMA assignments are not allowed.")
        m = _PRAGMA_RE.match(shape)
        if not m:
            raise QueryRejected("Malformed PRAGMA statement.")
        name = m.group("name").lower()
        if name not in READ_ONLY_PRAGMAS:
            raise QueryRejected(f"PRAGMA {name} is not on the read-only allowlist.")
        if m.group("arg") and name not in PRAGMAS_WITH_ARG:
            raise QueryRejected(f"PRAGMA {name} does not take an argument here.")

    return sql.strip()


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def _format_cell(value: Any, max_chars: int = MAX_CELL_CHARS) -> str:
    if value is None:
        text = "NULL"
    elif isinstance(value, (bytes, bytearray, memoryview)):
        text = f"<blob {len(bytes(value))} bytes>"
    else:
        text = str(value)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text.replace("|", "\\|")


def to_markdown_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Render rows as a compact GitHub-flavoured markdown table."""
    header = "| " + " | ".join(_format_cell(c) for c in columns) + " |"
    divider = "|" + "|".join("---" for _ in columns) + "|"
    body = ["| " + " | ".join(_format_cell(v) for v in row) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
# Schema helpers
# --------------------------------------------------------------------------- #


def _user_objects(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _require_object(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    """Look ``name`` up in sqlite_master (case-insensitive) and return (name, type)."""
    if not isinstance(name, str) or not name.strip():
        raise QueryRejected("Table name is required.")
    row = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name = ? COLLATE NOCASE",
        (name.strip(),),
    ).fetchone()
    if row is None:
        known = ", ".join(n for n, _ in _user_objects(conn)) or "(none)"
        raise QueryRejected(f"Unknown table or view: {name!r}. Known: {known}.")
    return row[0], row[1]


def _row_count(conn: sqlite3.Connection, name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(name)}").fetchone()[0])


def _foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"PRAGMA foreign_key_list({quote_identifier(table)})").fetchall()
    # id, seq, table, from, to, on_update, on_delete, match
    return [
        {"from": r[3], "table": r[2], "to": r[4], "on_update": r[5], "on_delete": r[6]}
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


def list_tables(db_path: str | os.PathLike[str]) -> str:
    """Markdown table of every user table and view with its row count."""
    with connect_readonly(db_path) as conn:
        objects = _user_objects(conn)
        if not objects:
            return "The database has no tables or views."
        rows = [(name, kind, _row_count(conn, name)) for name, kind in objects]
    return to_markdown_table(["name", "type", "rows"], rows)


def describe_table(db_path: str | os.PathLike[str], name: str) -> str:
    """Columns, types, row count, indexes and foreign keys for one table."""
    with connect_readonly(db_path) as conn:
        table, kind = _require_object(conn, name)
        qname = quote_identifier(table)
        cols = conn.execute(f"PRAGMA table_info({qname})").fetchall()
        count = _row_count(conn, table)
        fks = _foreign_keys(conn, table)
        fk_by_col = {fk["from"]: f"{fk['table']}.{fk['to']}" for fk in fks}

        index_rows = []
        for idx in conn.execute(f"PRAGMA index_list({qname})").fetchall():
            # seq, name, unique, origin, partial
            idx_name, unique, origin = idx[1], idx[2], idx[3]
            idx_cols = [
                c[2] for c in conn.execute(f"PRAGMA index_info({quote_identifier(idx_name)})").fetchall()
            ]
            origin_label = {"c": "CREATE INDEX", "u": "UNIQUE constraint", "pk": "PRIMARY KEY"}.get(
                origin, origin
            )
            index_rows.append((idx_name, ", ".join(str(c) for c in idx_cols), "yes" if unique else "no", origin_label))

    pk_cols = [c for c in cols if c[5]]
    # A lone INTEGER PRIMARY KEY is the rowid and can never be NULL.
    rowid_alias = pk_cols[0][1] if len(pk_cols) == 1 and (pk_cols[0][2] or "").upper() == "INTEGER" else None
    col_rows = [
        (
            c[1],
            c[2] or "ANY",
            "no" if c[3] or c[1] == rowid_alias else "yes",
            "" if c[4] is None else c[4],
            (f"PK{c[5]}" if len(pk_cols) > 1 else "PK") if c[5] else "",
            fk_by_col.get(c[1], ""),
        )
        for c in cols
    ]

    parts = [
        f"## {table} ({kind}, {count} rows)",
        "",
        "### Columns",
        to_markdown_table(["column", "type", "nullable", "default", "key", "references"], col_rows),
    ]
    if index_rows:
        parts += ["", "### Indexes", to_markdown_table(["index", "columns", "unique", "origin"], index_rows)]
    else:
        parts += ["", "### Indexes", "_none_"]
    if fks:
        fk_rows = [(fk["from"], f"{fk['table']}.{fk['to']}", fk["on_delete"], fk["on_update"]) for fk in fks]
        parts += ["", "### Foreign keys", to_markdown_table(["column", "references", "on delete", "on update"], fk_rows)]
    else:
        parts += ["", "### Foreign keys", "_none_"]
    return "\n".join(parts)


def _run_readonly(
    db_path: str | os.PathLike[str],
    statement: str,
    limit: int,
    timeout_seconds: float,
) -> tuple[list[str] | None, list[tuple[Any, ...]], bool]:
    """Execute an already-validated statement. Returns (columns, rows, truncated)."""
    with connect_readonly(db_path, timeout_seconds=timeout_seconds) as conn:
        try:
            cur = conn.execute(statement)
            if cur.description is None:
                return None, [], False
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit + 1)
        except sqlite3.DatabaseError as exc:
            msg = str(exc)
            if "interrupted" in msg:
                raise QueryRejected(f"Query took longer than {timeout_seconds:g}s and was stopped.") from exc
            if "not authorized" in msg or "prohibited" in msg:
                raise QueryRejected(f"Blocked by the read-only authorizer: {msg}.") from exc
            raise QueryRejected(f"SQLite error: {msg}") from exc
    return columns, rows[:limit], len(rows) > limit


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise QueryRejected(f"{name} must be an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise QueryRejected(f"{name} must be an integer.") from exc


def _row_footer(count: int, truncated: bool, hint: str) -> str:
    if truncated:
        return f"_Showing the first {count} rows; more are available. {hint}_"
    return f"_{count} row{'s' if count != 1 else ''}._"


def query(
    db_path: str | os.PathLike[str],
    sql: str,
    limit: int = DEFAULT_LIMIT,
    timeout_seconds: float = QUERY_TIMEOUT_SECONDS,
) -> str:
    """Run one read-only statement and return at most ``limit`` rows as markdown."""
    statement = validate_readonly_sql(sql)
    limit = max(1, min(_as_int(limit, "limit"), MAX_ROWS))
    columns, rows, truncated = _run_readonly(db_path, statement, limit, timeout_seconds)
    if columns is None:
        return "_Statement returned no columns._"
    hint = f"Raise `limit` (max {MAX_ROWS}) or narrow the query."
    return to_markdown_table(columns, rows) + "\n\n" + _row_footer(len(rows), truncated, hint)


def sample_rows(db_path: str | os.PathLike[str], table: str, n: int = 5) -> str:
    """The first ``n`` rows of a table or view (max 50) as markdown."""
    n = max(1, min(_as_int(n, "n"), MAX_SAMPLE_ROWS))
    with connect_readonly(db_path) as conn:
        name, _ = _require_object(conn, table)
    statement = f"SELECT * FROM {quote_identifier(name)}"
    columns, rows, truncated = _run_readonly(db_path, statement, n, QUERY_TIMEOUT_SECONDS)
    hint = f"Use a larger `n` (max {MAX_SAMPLE_ROWS}) or the query tool."
    return to_markdown_table(columns or [], rows) + "\n\n" + _row_footer(len(rows), truncated, hint)


_MERMAID_SAFE = re.compile(r"[^A-Za-z0-9_]")


def _mermaid_name(name: str) -> str:
    safe = _MERMAID_SAFE.sub("_", name)
    return safe if safe and not safe[0].isdigit() else f"t_{safe}"


def _mermaid_type(declared: str) -> str:
    base = re.split(r"[\s(]", declared.strip(), maxsplit=1)[0] if declared.strip() else "ANY"
    return _MERMAID_SAFE.sub("_", base).upper() or "ANY"


def schema_diagram(db_path: str | os.PathLike[str]) -> str:
    """A Mermaid ``erDiagram`` of every table and its foreign keys."""
    lines = ["erDiagram"]
    relations: list[str] = []
    with connect_readonly(db_path) as conn:
        tables = [n for n, kind in _user_objects(conn) if kind == "table"]
        for table in tables:
            cols = conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
            fks = _foreign_keys(conn, table)
            fk_cols = {fk["from"] for fk in fks}
            lines.append(f"    {_mermaid_name(table)} {{")
            for c in cols:
                keys = [k for k, on in (("PK", c[5] > 0), ("FK", c[1] in fk_cols)) if on]
                suffix = f" {','.join(keys)}" if keys else ""
                lines.append(f"        {_mermaid_type(c[2])} {_mermaid_name(c[1])}{suffix}")
            lines.append("    }")
            for fk in fks:
                label = str(fk["from"]).replace('"', "'")
                relations.append(f'    {_mermaid_name(fk["table"])} ||--o{{ {_mermaid_name(table)} : "{label}"')
    return "\n".join(lines + relations)


def schema_overview(db_path: str | os.PathLike[str]) -> str:
    """Every table and view with its CREATE statement (for the schema://tables resource)."""
    with connect_readonly(db_path) as conn:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    if not rows:
        return "The database has no tables or views."
    parts = [f"# Schema of {Path(db_path).name}", ""]
    for kind, name, sql in rows:
        parts += [f"## {name} ({kind})", "```sql", (sql or "").strip(), "```", ""]
    return "\n".join(parts).rstrip() + "\n"
