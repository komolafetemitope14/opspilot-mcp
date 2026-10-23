"""Diagnostic MCP tools that correlate observations from infrastructure clients."""

from typing import Any

from mcp.server import MCPServer

from opspilot.clients.kubernetes import KubernetesClient
from opspilot.diagnostics.kubernetes import KubernetesDiagnostics


def register_diagnostic_tools(mcp: MCPServer) -> None:
    """Register deterministic diagnostic tools on an MCP server."""

    @mcp.tool()
    def diagnose_kubernetes_issues(
        name: str,
        namespace: str = "default",
    ) -> dict[str, Any]:
        """Diagnose a Kubernetes Pod using its state and related events."""
        client = KubernetesClient()

        pod = client.inspect_pod(name, namespace)
        events = client.list_events(namespace)

        pod_events = [
            event
            for event in events
            if event["kind"] == "Pod"
            and event["name"] == name
        ]

        diagnostics = KubernetesDiagnostics()

        return diagnostics.diagnose_pod(
            pod,
            pod_events,
        )
