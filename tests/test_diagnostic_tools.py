from unittest.mock import Mock, patch

from mcp.server import MCPServer

from opspilot.tools.diagnostics import register_diagnostic_tools


def make_pod():
    return {
        "name": "broken-pod",
        "namespace": "default",
        "phase": "Pending",
        "ready": False,
        "restarts": 0,
        "node": "opspilot-control-plane",
        "pod_ip": "10.244.0.10",
    }


def make_event(
    *,
    name="broken-pod",
    namespace="default",
    kind="Pod",
    event_type="Warning",
    reason="Failed",
    message="Error: ImagePullBackOff",
):
    return {
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "type": event_type,
        "reason": reason,
        "message": message,
        "count": 1,
        "first_timestamp": None,
        "last_timestamp": None,
    }


def get_tool_function(mcp, name):
    tool = mcp._tool_manager._tools[name]
    return tool.fn


def test_registers_kubernetes_diagnostic_tool():
    mcp = MCPServer("test")

    register_diagnostic_tools(mcp)

    assert "diagnose_kubernetes_issues" in mcp._tool_manager._tools


@patch("opspilot.tools.diagnostics.KubernetesDiagnostics")
@patch("opspilot.tools.diagnostics.KubernetesClient")
def test_diagnose_kubernetes_issues_uses_default_namespace(
    mock_client_class,
    mock_diagnostics_class,
):
    client = Mock()
    diagnostics = Mock()

    mock_client_class.return_value = client
    mock_diagnostics_class.return_value = diagnostics

    pod = make_pod()
    events = [make_event()]

    client.inspect_pod.return_value = pod
    client.list_events.return_value = events

    expected = {
        "status": "unhealthy",
        "system": "kubernetes",
        "resource": {
            "kind": "Pod",
            "name": "broken-pod",
            "namespace": "default",
        },
        "findings": [],
    }
    diagnostics.diagnose_pod.return_value = expected

    mcp = MCPServer("test")
    register_diagnostic_tools(mcp)

    tool = get_tool_function(mcp, "diagnose_kubernetes_issues")
    result = tool("broken-pod")

    client.inspect_pod.assert_called_once_with(
        "broken-pod",
        "default",
    )
    client.list_events.assert_called_once_with("default")
    diagnostics.diagnose_pod.assert_called_once_with(
        pod,
        events,
    )
    assert result == expected


@patch("opspilot.tools.diagnostics.KubernetesDiagnostics")
@patch("opspilot.tools.diagnostics.KubernetesClient")
def test_diagnose_kubernetes_issues_uses_requested_namespace(
    mock_client_class,
    mock_diagnostics_class,
):
    client = Mock()
    diagnostics = Mock()

    mock_client_class.return_value = client
    mock_diagnostics_class.return_value = diagnostics

    pod = make_pod()
    pod["namespace"] = "production"

    events = [
        make_event(namespace="production"),
    ]

    client.inspect_pod.return_value = pod
    client.list_events.return_value = events
    diagnostics.diagnose_pod.return_value = {
        "status": "unhealthy",
        "system": "kubernetes",
        "resource": {
            "kind": "Pod",
            "name": "broken-pod",
            "namespace": "production",
        },
        "findings": [],
    }

    mcp = MCPServer("test")
    register_diagnostic_tools(mcp)

    tool = get_tool_function(mcp, "diagnose_kubernetes_issues")
    tool("broken-pod", "production")

    client.inspect_pod.assert_called_once_with(
        "broken-pod",
        "production",
    )
    client.list_events.assert_called_once_with("production")


@patch("opspilot.tools.diagnostics.KubernetesDiagnostics")
@patch("opspilot.tools.diagnostics.KubernetesClient")
def test_diagnose_kubernetes_issues_filters_unrelated_events(
    mock_client_class,
    mock_diagnostics_class,
):
    client = Mock()
    diagnostics = Mock()

    mock_client_class.return_value = client
    mock_diagnostics_class.return_value = diagnostics

    pod = make_pod()

    matching_event = make_event()

    other_pod_event = make_event(
        name="another-pod",
        message="Another Pod warning",
    )

    service_event = make_event(
        kind="Service",
        message="Service warning",
    )

    client.inspect_pod.return_value = pod
    client.list_events.return_value = [
        matching_event,
        other_pod_event,
        service_event,
    ]

    diagnostics.diagnose_pod.return_value = {
        "status": "unhealthy",
        "system": "kubernetes",
        "resource": {
            "kind": "Pod",
            "name": "broken-pod",
            "namespace": "default",
        },
        "findings": [],
    }

    mcp = MCPServer("test")
    register_diagnostic_tools(mcp)

    tool = get_tool_function(mcp, "diagnose_kubernetes_issues")
    tool("broken-pod")

    diagnostics.diagnose_pod.assert_called_once_with(
        pod,
        [matching_event],
    )
