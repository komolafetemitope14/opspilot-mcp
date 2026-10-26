from unittest.mock import Mock, patch, call

import pytest

from mcp.server import MCPServer

from opspilot.tools.diagnostics import register_diagnostic_tools


def make_container():
    return {
        "id": "abc123",
        "name": "broken-container",
        "image": "example:latest",
        "status": "exited",
        "health": None,
        "running": False,
        "exit_code": 7,
        "ports": {},
    }


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


def test_registers_docker_diagnostic_tool():
    mcp = MCPServer("test")

    register_diagnostic_tools(mcp)

    assert "diagnose_docker_issues" in mcp._tool_manager._tools


@patch("opspilot.tools.diagnostics.DockerDiagnostics")
@patch("opspilot.tools.diagnostics.DockerClient")
def test_diagnose_docker_issues(
    mock_client_class,
    mock_diagnostics_class,
):
    client = Mock()
    diagnostics = Mock()

    mock_client_class.return_value = client
    mock_diagnostics_class.return_value = diagnostics

    container = make_container()
    client.inspect_container.return_value = container

    expected = {
        "status": "unhealthy",
        "system": "docker",
        "resource": {
            "kind": "Container",
            "name": "broken-container",
        },
        "findings": [],
    }
    diagnostics.diagnose_container.return_value = expected

    mcp = MCPServer("test")
    register_diagnostic_tools(mcp)

    tool = get_tool_function(mcp, "diagnose_docker_issues")
    result = tool("broken-container")

    client.inspect_container.assert_called_once_with("broken-container")
    assert client.method_calls == [call.inspect_container("broken-container")]
    diagnostics.diagnose_container.assert_called_once_with(container)
    assert result == expected


def test_registers_postgres_diagnostic_tool():
    mcp = MCPServer("test")

    register_diagnostic_tools(mcp)

    assert "diagnose_postgres_issues" in mcp._tool_manager._tools


@patch("opspilot.tools.diagnostics.PostgresDiagnostics")
@patch("opspilot.tools.diagnostics.PostgresClient")
def test_diagnose_postgres_issues(
    mock_client_class,
    mock_diagnostics_class,
):
    client = Mock()
    diagnostics = Mock()

    mock_client_class.return_value = client
    mock_diagnostics_class.return_value = diagnostics

    client.health_check.return_value = True
    client.list_tables.return_value = ["services"]

    expected = {
        "status": "healthy",
        "system": "postgresql",
        "resource": {
            "kind": "Database",
        },
        "findings": [],
    }
    diagnostics.diagnose_database.return_value = expected

    mcp = MCPServer("test")
    register_diagnostic_tools(mcp)

    tool = get_tool_function(mcp, "diagnose_postgres_issues")
    result = tool()

    client.health_check.assert_called_once_with()
    client.list_tables.assert_called_once_with()
    diagnostics.diagnose_database.assert_called_once_with(
        healthy=True,
        tables=["services"],
    )
    assert result == expected


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


def test_registers_cross_system_diagnostic_tool():
    mcp = MCPServer("test")

    register_diagnostic_tools(mcp)

    assert "diagnose_cross_system_issues" in mcp._tool_manager._tools


@patch("opspilot.tools.diagnostics.CorrelationDiagnostics")
@patch("opspilot.tools.diagnostics.PostgresDiagnostics")
@patch("opspilot.tools.diagnostics.PostgresClient")
@patch("opspilot.tools.diagnostics.KubernetesDiagnostics")
@patch("opspilot.tools.diagnostics.KubernetesClient")
@patch("opspilot.tools.diagnostics.load_kubernetes_settings")
@patch("opspilot.tools.diagnostics.load_postgres_config")
def test_diagnose_cross_system_issues(
    mock_postgres_settings,
    mock_kubernetes_settings,
    mock_kubernetes_client_class,
    mock_kubernetes_diagnostics_class,
    mock_postgres_client_class,
    mock_postgres_diagnostics_class,
    mock_correlation_diagnostics_class,
):
    kubernetes_client = Mock()
    kubernetes_diagnostics = Mock()
    postgres_client = Mock()
    postgres_diagnostics = Mock()
    correlation_diagnostics = Mock()

    mock_kubernetes_client_class.return_value = kubernetes_client
    mock_kubernetes_diagnostics_class.return_value = kubernetes_diagnostics
    mock_postgres_client_class.return_value = postgres_client
    mock_postgres_diagnostics_class.return_value = postgres_diagnostics
    mock_correlation_diagnostics_class.return_value = correlation_diagnostics

    pod = make_pod()
    matching_event = make_event()
    other_pod_event = make_event(
        name="another-pod",
        message="Another Pod warning",
    )

    kubernetes_client.inspect_pod.return_value = pod
    kubernetes_client.list_events.return_value = [
        matching_event,
        other_pod_event,
    ]

    kubernetes = {
        "status": "unhealthy",
        "system": "kubernetes",
        "resource": {
            "kind": "Pod",
            "name": "broken-pod",
            "namespace": "default",
        },
        "findings": [],
    }
    kubernetes_diagnostics.diagnose_pod.return_value = kubernetes

    postgres_client.health_check.return_value = False
    postgres_client.list_tables.return_value = ["services"]

    postgres = {
        "status": "unhealthy",
        "system": "postgresql",
        "resource": {
            "kind": "Database",
        },
        "findings": [],
    }
    postgres_diagnostics.diagnose_database.return_value = postgres

    expected = {
        "status": "unhealthy",
        "system": "cross-system",
        "resource": {
            "kind": "Correlation",
        },
        "findings": [
            {
                "code": "possible_database_dependency_failure",
                "severity": "error",
                "summary": "Kubernetes and PostgreSQL are unhealthy.",
                "evidence": [
                    "Kubernetes diagnosis is unhealthy.",
                    "PostgreSQL diagnosis is unhealthy.",
                ],
                "recommendation": "Check the application's database connection.",
            },
        ],
    }
    correlation_diagnostics.correlate.return_value = expected

    mcp = MCPServer("test")
    register_diagnostic_tools(mcp)

    tool = get_tool_function(mcp, "diagnose_cross_system_issues")
    result = tool("broken-pod")

    kubernetes_client.inspect_pod.assert_called_once_with(
        "broken-pod",
        "default",
    )
    kubernetes_client.list_events.assert_called_once_with("default")
    kubernetes_diagnostics.diagnose_pod.assert_called_once_with(
        pod,
        [matching_event],
    )
    postgres_client.health_check.assert_called_once_with()
    postgres_client.list_tables.assert_called_once_with()
    postgres_diagnostics.diagnose_database.assert_called_once_with(
        healthy=False,
        tables=["services"],
    )
    correlation_diagnostics.correlate.assert_called_once_with(
        kubernetes=kubernetes,
        postgres=postgres,
    )
    assert result == expected


@pytest.mark.parametrize("invalid", ["kubernetes", "postgres"])
def test_cross_system_validates_both_before_activation(monkeypatch, invalid):
    from opspilot.tools import diagnostics as module
    from opspilot.configuration import ConfigurationError
    timeline = []

    def settings(backend):
        timeline.append(backend)
        if backend == invalid:
            raise ConfigurationError("kubeconfig is invalid" if backend == "kubernetes" else "POSTGRES_PORT is invalid")
        return object()

    monkeypatch.setattr(module, "load_kubernetes_settings", lambda: settings("kubernetes"))
    monkeypatch.setattr(module, "load_postgres_config", lambda: settings("postgres"))
    activate = Mock(side_effect=AssertionError("Must not activate either backend"))
    monkeypatch.setattr(module, "KubernetesClient", activate)
    monkeypatch.setattr(module, "PostgresClient", activate)
    server = MCPServer("test")
    register_diagnostic_tools(server)
    with pytest.raises(ConfigurationError):
        get_tool_function(server, "diagnose_cross_system_issues")("pod")
    assert timeline == (["kubernetes"] if invalid == "kubernetes" else ["kubernetes", "postgres"])
    activate.assert_not_called()


def test_cross_system_passes_validated_snapshots_before_observation(monkeypatch):
    from opspilot.tools import diagnostics as module
    timeline = []
    kube_settings, pg_settings = object(), object()
    monkeypatch.setattr(module, "load_kubernetes_settings", lambda: timeline.append("validate kube") or kube_settings)
    monkeypatch.setattr(module, "load_postgres_config", lambda: timeline.append("validate pg") or pg_settings)
    kube = Mock()
    kube.inspect_pod.return_value = make_pod()
    kube.list_events.return_value = []
    pg = Mock()
    pg.health_check.return_value = True
    pg.list_tables.return_value = []

    def activate_kube(settings):
        assert timeline == ["validate kube", "validate pg"]
        assert settings is kube_settings
        timeline.append("activate kube")
        return kube

    def activate_pg(settings):
        assert settings is pg_settings
        return pg

    monkeypatch.setattr(module, "KubernetesClient", activate_kube)
    monkeypatch.setattr(module, "PostgresClient", activate_pg)
    server = MCPServer("test")
    register_diagnostic_tools(server)
    get_tool_function(server, "diagnose_cross_system_issues")("pod")
    assert timeline == ["validate kube", "validate pg", "activate kube"]
