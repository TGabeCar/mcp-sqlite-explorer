"""End-to-end: talk to the server through a real MCP client session (in memory)."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_sqlite_explorer import server

EXPECTED_TOOLS = {"list_tables", "describe_table", "query", "sample_rows", "schema_diagram"}


def _text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def test_mcp_client_sees_tools_resource_and_read_only_hints(demo_db: Path) -> None:
    app = server.build_server(str(demo_db))

    async def scenario():
        async with create_connected_server_and_client_session(app._mcp_server) as client:
            tools = (await client.list_tools()).tools
            resources = (await client.list_resources()).resources
            listing = await client.call_tool("list_tables", {})
            good = await client.call_tool("query", {"sql": "SELECT COUNT(*) AS n FROM missions"})
            bad = await client.call_tool("query", {"sql": "DROP TABLE missions"})
            schema = await client.read_resource("schema://tables")
            return tools, resources, listing, good, bad, schema

    tools, resources, listing, good, bad, schema = anyio.run(scenario)

    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert all(t.annotations and t.annotations.readOnlyHint for t in tools)
    assert [str(r.uri) for r in resources] == ["schema://tables"]
    assert "| spacecraft | table | 6 |" in _text(listing)
    assert "| 24 |" in _text(good) and not good.isError
    assert bad.isError and "DROP" in _text(bad)
    assert "CREATE TABLE missions" in schema.contents[0].text


def test_cli_uses_env_var_when_no_argument(monkeypatch: pytest.MonkeyPatch, demo_db: Path) -> None:
    started: list[str] = []
    monkeypatch.setenv(server.ENV_VAR, str(demo_db))
    monkeypatch.setattr(server.FastMCP, "run", lambda self, *a, **k: started.append(self.name))
    server.main([])
    assert started == ["sqlite-explorer"]


def test_cli_errors_clearly_without_a_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(server.ENV_VAR, raising=False)
    with pytest.raises(SystemExit, match="SQLITE_DB_PATH"):
        server.main([])
    with pytest.raises(SystemExit, match="not found"):
        server.main([str(tmp_path / "nope.db")])
