"""Read-only Kubernetes MCP tools backed by the synchronous Kubernetes client."""

from typing import Any

from mcp.server import MCPServer
from opspilot.limits import bounded_tool_result, LogTail, DEFAULT_LOG_TAIL

from opspilot.clients.kubernetes import KubernetesClient


def register_kubernetes_tools(mcp: MCPServer) -> None:
    """Register the six read-only Kubernetes tools on an MCP server."""

    @mcp.tool()
    @bounded_tool_result
    def kubernetes_health() -> bool:
        """Check whether the Kubernetes API is reachable."""
        return KubernetesClient().health_check()

    @mcp.tool()
    @bounded_tool_result
    def kubernetes_list_namespaces() -> list[str]:
        """List Kubernetes namespace names."""
        return KubernetesClient().list_namespaces()

    @mcp.tool()
    @bounded_tool_result
    def kubernetes_list_pods(namespace: str | None = None) -> list[dict[str, Any]]:
        """List Pod summaries in one namespace or all namespaces."""
        return KubernetesClient().list_pods(namespace)

    @mcp.tool()
    @bounded_tool_result
    def kubernetes_inspect_pod(name: str, namespace: str = "default") -> dict[str, Any]:
        """Inspect the named Kubernetes Pod."""
        return KubernetesClient().inspect_pod(name, namespace)

    @mcp.tool()
    @bounded_tool_result(logs=True)
    def kubernetes_pod_logs(name: str, namespace: str = "default", tail: LogTail = DEFAULT_LOG_TAIL) -> str:
        """Read the last tail log lines from the named Kubernetes Pod."""
        return KubernetesClient().pod_logs(name, namespace, tail)

    @mcp.tool()
    @bounded_tool_result
    def kubernetes_list_events(namespace: str | None = None) -> list[dict[str, Any]]:
        """List Event summaries in one namespace or all namespaces."""
        return KubernetesClient().list_events(namespace)
