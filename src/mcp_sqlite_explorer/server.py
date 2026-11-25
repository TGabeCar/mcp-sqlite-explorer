"""MCP server: wires the plain functions in :mod:`mcp_sqlite_explorer.db` to FastMCP."""

from __future__ import annotations

import argparse
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__, db

ENV_VAR = "SQLITE_DB_PATH"

INSTRUCTIONS = (
    "Read-only explorer for one SQLite database. Start with list_tables or "
    "schema_diagram, use describe_table before writing SQL, and use query for "
    "SELECT/WITH/EXPLAIN/read-only PRAGMA statements. Writes are impossible."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def build_server(db_path: str) -> FastMCP:
    """Create a FastMCP server bound to ``db_path``."""
    db.resolve_db_path(db_path)  # fail fast if the file is missing
    server = FastMCP("sqlite-explorer", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ_ONLY)
    def list_tables() -> str:
        """List every table and view in the database with its row count."""
        return db.list_tables(db_path)

    @server.tool(annotations=READ_ONLY)
    def describe_table(name: str) -> str:
        """Describe one table: columns, types, nullability, defaults, primary key,
        row count, indexes and foreign keys."""
        return db.describe_table(db_path, name)

    @server.tool(annotations=READ_ONLY)
    def query(sql: str, limit: int = db.DEFAULT_LIMIT) -> str:
        """Run ONE read-only SQL statement (SELECT, WITH, EXPLAIN or a read-only
        PRAGMA) and return the rows as a markdown table. `limit` caps the rows
        returned (default 100, max 1000); long cells are truncated. Anything that
        could write is rejected."""
        return db.query(db_path, sql, limit)

    @server.tool(annotations=READ_ONLY)
    def sample_rows(table: str, n: int = 5) -> str:
        """Return the first `n` rows of a table or view (default 5, max 50)."""
        return db.sample_rows(db_path, table, n)

    @server.tool(annotations=READ_ONLY)
    def schema_diagram() -> str:
        """Return a Mermaid erDiagram of all tables and their foreign keys."""
        return db.schema_diagram(db_path)

    @server.resource("schema://tables", mime_type="text/markdown")
    def schema_tables() -> str:
        """Every table and view with its CREATE statement."""
        return db.schema_overview(db_path)

    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-sqlite-explorer",
        description="Read-only MCP server for exploring a SQLite database (stdio transport).",
    )
    parser.add_argument(
        "db_path",
        nargs="?",
        help=f"Path to the SQLite file. Falls back to the {ENV_VAR} environment variable.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    db_path = args.db_path or os.environ.get(ENV_VAR)
    if not db_path:
        sys.exit(f"mcp-sqlite-explorer: pass a database path or set {ENV_VAR}.")
    try:
        server = build_server(db_path)
    except db.DatabaseNotFound as exc:
        sys.exit(f"mcp-sqlite-explorer: {exc}")
    server.run()  # stdio


if __name__ == "__main__":  # pragma: no cover
    main()
