"""Read-only Docker MCP tools backed by the synchronous Docker client."""

from typing import Any

from mcp.server import MCPServer
from opspilot.limits import bounded_tool_result, LogTail, DEFAULT_LOG_TAIL

from opspilot.clients.docker import DockerClient


def register_docker_tools(mcp: MCPServer) -> None:
    """Register the four read-only Docker tools on an MCP server."""

    @mcp.tool()
    @bounded_tool_result
    def docker_health() -> bool:
        """Check whether the Docker Engine is reachable."""
        return DockerClient().health_check()

    @mcp.tool()
    @bounded_tool_result
    def docker_list_containers() -> list[dict[str, Any]]:
        """List curated summaries of running and stopped Docker containers."""
        return DockerClient().list_containers()

    @mcp.tool()
    @bounded_tool_result
    def docker_inspect_container(name_or_id: str) -> dict[str, Any]:
        """Inspect curated operational details for a Docker container."""
        return DockerClient().inspect_container(name_or_id)

    @mcp.tool()
    @bounded_tool_result(logs=True)
    def docker_container_logs(name_or_id: str, tail: LogTail = DEFAULT_LOG_TAIL) -> str:
        """Read the last tail log lines from a Docker container."""
        return DockerClient().container_logs(name_or_id, tail)
