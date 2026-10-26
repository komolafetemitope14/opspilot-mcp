"""PostgreSQL MCP tools backed by the synchronous database client."""

from typing import Any

from mcp.server import MCPServer
from opspilot.limits import bounded_tool_result

from opspilot.clients.postgres import PostgresClient


def register_postgres_tools(mcp: MCPServer) -> None:
    """Register the three PostgreSQL tools on an MCP server."""

    @mcp.tool()
    @bounded_tool_result
    def postgres_health() -> bool:
        """Check whether PostgreSQL is reachable."""
        return PostgresClient().health_check()

    @mcp.tool()
    @bounded_tool_result
    def postgres_list_tables() -> list[str]:
        """List public-schema PostgreSQL tables."""
        return PostgresClient().list_tables()

    @mcp.tool()
    @bounded_tool_result
    def postgres_query(query: str) -> list[dict[str, Any]]:
        """Execute a single SELECT statement in a read-only transaction."""
        return PostgresClient().execute_select(query)
