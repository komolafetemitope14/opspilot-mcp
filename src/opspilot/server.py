"""OpsPilot MCP server entry point."""

from mcp.server import MCPServer

from opspilot.tools.docker import register_docker_tools
from opspilot.tools.postgres import register_postgres_tools

mcp = MCPServer("OpsPilot")
register_postgres_tools(mcp)
register_docker_tools(mcp)


def main() -> None:
    """Run OpsPilot over standard input and output."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
