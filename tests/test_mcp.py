"""Exercise tool discovery and calls through the MCP v2 client."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import docker as docker_sdk
from kubernetes.client.exceptions import ApiException
from mcp import Client
from mcp.client.stdio import StdioServerParameters
import psycopg
import pytest

from opspilot.server import mcp
from opspilot.tools import docker, kubernetes, postgres


def run(coroutine):
    async def bounded():
        async with asyncio.timeout(15):
            return await coroutine

    return asyncio.run(bounded())


def assert_tools(tools):
    by_name = {tool.name: tool for tool in tools}
    assert len(tools) == 17
    assert set(by_name) == {
        "postgres_health", "postgres_list_tables", "postgres_query",
        "docker_health", "docker_list_containers", "docker_inspect_container",
        "docker_container_logs",
        "kubernetes_health", "kubernetes_list_namespaces", "kubernetes_list_pods",
        "kubernetes_inspect_pod", "kubernetes_pod_logs", "kubernetes_list_events",
        "diagnose_kubernetes_issues",
        "diagnose_docker_issues",
        "diagnose_postgres_issues",
        "diagnose_cross_system_issues",
    }
    for name in ("postgres_health", "postgres_list_tables", "docker_health", "docker_list_containers",
                 "kubernetes_health", "kubernetes_list_namespaces", "diagnose_postgres_issues"):
        schema = by_name[name].input_schema
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema.get("required", []) == []
    schema = by_name["postgres_query"].input_schema
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"query"}
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["required"] == ["query"]
    for name in ("docker_inspect_container", "docker_container_logs"):
        schema = by_name[name].input_schema
        assert schema["type"] == "object"
        expected = {"name_or_id", "tail"} if name == "docker_container_logs" else {"name_or_id"}
        assert set(schema["properties"]) == expected
        assert schema["properties"]["name_or_id"]["type"] == "string"
        assert schema["required"] == ["name_or_id"]
    tail = by_name["docker_container_logs"].input_schema["properties"]["tail"]
    assert tail["type"] == "integer"
    assert tail["default"] == 100
    assert tail["minimum"] == 1
    assert tail["maximum"] == 1000
    for name in ("kubernetes_list_pods", "kubernetes_list_events"):
        schema = by_name[name].input_schema
        assert schema["type"] == "object"
        assert set(schema["properties"]) == {"namespace"}
        assert schema.get("required", []) == []
        namespace = schema["properties"]["namespace"]
        assert namespace["default"] is None
        assert {option["type"] for option in namespace["anyOf"]} == {"string", "null"}
    for name in ("kubernetes_inspect_pod", "kubernetes_pod_logs",
                 "diagnose_kubernetes_issues", "diagnose_cross_system_issues"):
        schema = by_name[name].input_schema
        assert schema["type"] == "object"
        expected = {"name", "namespace", "tail"} if name == "kubernetes_pod_logs" else {"name", "namespace"}
        assert set(schema["properties"]) == expected
        assert schema["required"] == ["name"]
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["namespace"]["type"] == "string"
        assert schema["properties"]["namespace"]["default"] == "default"
    tail = by_name["kubernetes_pod_logs"].input_schema["properties"]["tail"]
    assert tail["type"] == "integer"
    assert tail["default"] == 100
    assert tail["minimum"] == 1
    assert tail["maximum"] == 1000


def test_discovery_without_database_configuration(monkeypatch):
    factory = MagicMock(side_effect=AssertionError("Discovery must not connect"))
    monkeypatch.setattr(postgres, "PostgresClient", factory)
    monkeypatch.setattr(docker, "DockerClient", factory)
    monkeypatch.setattr(kubernetes, "KubernetesClient", factory)

    async def check():
        async with Client(mcp) as client:
            assert_tools((await client.list_tools()).tools)

    run(check())
    factory.assert_not_called()


@pytest.mark.parametrize("name,arguments,method,value", [
    ("postgres_health", {}, "health_check", True),
    ("postgres_health", {}, "health_check", False),
    ("postgres_list_tables", {}, "list_tables", ["alpha", "beta"]),
    ("postgres_list_tables", {}, "list_tables", []),
    ("postgres_query", {"query": "SELECT 42 AS answer"}, "execute_select", [{"answer": 42}]),
    ("postgres_query", {"query": "SELECT NULL AS value"}, "execute_select", [{"value": None}]),
    ("postgres_query", {"query": "SELECT 1 WHERE false"}, "execute_select", []),
])
def test_delegation_and_results(monkeypatch, name, arguments, method, value):
    factory = MagicMock()
    getattr(factory.return_value, method).return_value = value
    monkeypatch.setattr(postgres, "PostgresClient", factory)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments)
            assert not result.is_error
            assert result.structured_content == {"result": value}

    run(check())
    factory.assert_called_once_with()
    getattr(factory.return_value, method).assert_called_once_with(*arguments.values())
    assert len(factory.return_value.method_calls) == 1


@pytest.mark.parametrize("query", ["", "DELETE FROM items", "SELECT 1; SELECT 2"])
def test_invalid_queries_are_tool_errors(monkeypatch, query):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    for key in ("HOST", "PORT", "DB", "USER", "PASSWORD"):
        monkeypatch.setenv(f"POSTGRES_{key}", "unused")
    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool("postgres_query", {"query": query})
            assert result.is_error
            assert "Error executing tool postgres_query" in result.content[0].text

    run(check())
    connect.assert_not_called()


def test_postgresql_error_is_tool_error(monkeypatch):
    factory = MagicMock()
    factory.return_value.execute_select.side_effect = psycopg.errors.SyntaxError("bad SQL")
    monkeypatch.setattr(postgres, "PostgresClient", factory)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool("postgres_query", {"query": "SELECT FROM"})
            assert result.is_error
            assert "Error executing tool postgres_query" in result.content[0].text

    run(check())
    factory.return_value.execute_select.assert_called_once_with("SELECT FROM")


@pytest.mark.parametrize("arguments", [{}, {"query": 42}])
def test_query_input_validation(monkeypatch, arguments):
    factory = MagicMock()
    monkeypatch.setattr(postgres, "PostgresClient", factory)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool("postgres_query", arguments)
            assert result.is_error

    run(check())
    factory.assert_not_called()


def test_stdio_module_startup_and_discovery():
    root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=str(root / ".venv/bin/python"),
        args=["-m", "opspilot.server"],
        cwd=str(root),
    )

    async def check():
        async with Client(parameters) as client:
            assert_tools((await client.list_tools()).tools)

    run(check())


@pytest.mark.parametrize("name,arguments,method,value,expected_args", [
    ("docker_health", {}, "health_check", True, ()),
    ("docker_health", {}, "health_check", False, ()),
    ("docker_list_containers", {}, "list_containers", [], ()),
    ("docker_list_containers", {}, "list_containers", [
        {"id": "abc123", "name": "web", "image": "nginx", "status": "running", "health": None},
    ], ()),
    ("docker_inspect_container", {"name_or_id": "web"}, "inspect_container", {
        "id": "abc123", "name": "web", "image": "nginx", "status": "running",
        "health": "healthy", "running": True, "exit_code": 0, "ports": {},
    }, ("web",)),
    ("docker_container_logs", {"name_or_id": "web"}, "container_logs", "ready café\n", ("web", 100)),
    ("docker_container_logs", {"name_or_id": "abc123", "tail": 25}, "container_logs", "", ("abc123", 25)),
])
def test_docker_delegation_and_results(monkeypatch, name, arguments, method, value, expected_args):
    factory = MagicMock()
    getattr(factory.return_value, method).return_value = value
    monkeypatch.setattr(docker, "DockerClient", factory)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments)
            assert not result.is_error
            expected = value if name == "docker_inspect_container" else {"result": value}
            assert result.structured_content == expected

    run(check())
    factory.assert_called_once_with()
    getattr(factory.return_value, method).assert_called_once_with(*expected_args)
    assert len(factory.return_value.method_calls) == 1


@pytest.mark.parametrize("name,arguments", [
    ("docker_inspect_container", {}),
    ("docker_inspect_container", {"name_or_id": 42}),
    ("docker_container_logs", {}),
    ("docker_container_logs", {"name_or_id": None}),
    ("docker_container_logs", {"name_or_id": "web", "tail": "invalid"}),
    ("docker_container_logs", {"name_or_id": "web", "tail": 1.5}),
    ("docker_container_logs", {"name_or_id": "web", "tail": None}),
])
def test_docker_input_validation(monkeypatch, name, arguments):
    factory = MagicMock()
    monkeypatch.setattr(docker, "DockerClient", factory)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments)
            assert result.is_error

    run(check())
    factory.assert_not_called()


@pytest.mark.parametrize("name,arguments,method", [
    ("docker_health", {}, "health_check"),
    ("docker_list_containers", {}, "list_containers"),
    ("docker_inspect_container", {"name_or_id": "missing"}, "inspect_container"),
    ("docker_container_logs", {"name_or_id": "missing"}, "container_logs"),
])
@pytest.mark.parametrize("construction_error", [False, True])
def test_docker_exceptions_are_tool_errors(monkeypatch, name, arguments, method, construction_error):
    factory = MagicMock()
    error = docker_sdk.errors.DockerException("Docker unavailable")
    if construction_error:
        factory.side_effect = error
    else:
        getattr(factory.return_value, method).side_effect = error
    monkeypatch.setattr(docker, "DockerClient", factory)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments)
            assert result.is_error
            assert f"Error executing tool {name}" in result.content[0].text

    run(check())
    factory.assert_called_once_with()
    if not construction_error:
        expected_args = ("missing", 100) if method == "container_logs" else tuple(arguments.values())
        getattr(factory.return_value, method).assert_called_once_with(*expected_args)


@pytest.mark.parametrize("name,arguments,method,value,expected_args", [
    ("kubernetes_health", {}, "health_check", True, ()),
    ("kubernetes_health", {}, "health_check", False, ()),
    ("kubernetes_list_namespaces", {}, "list_namespaces", ["default", "prod"], ()),
    ("kubernetes_list_namespaces", {}, "list_namespaces", [], ()),
    ("kubernetes_list_pods", {}, "list_pods", [{"name": "web", "namespace": "default", "node": None}], (None,)),
    ("kubernetes_list_pods", {"namespace": None}, "list_pods", [], (None,)),
    ("kubernetes_list_pods", {"namespace": "prod"}, "list_pods", [], ("prod",)),
    ("kubernetes_inspect_pod", {"name": "web"}, "inspect_pod", {"name": "web", "namespace": "default"}, ("web", "default")),
    ("kubernetes_inspect_pod", {"name": "web", "namespace": "prod"}, "inspect_pod", {"name": "web", "namespace": "prod"}, ("web", "prod")),
    ("kubernetes_pod_logs", {"name": "web"}, "pod_logs", "ready café\n", ("web", "default", 100)),
    ("kubernetes_pod_logs", {"name": "web", "namespace": "prod"}, "pod_logs", "ready\n", ("web", "prod", 100)),
    ("kubernetes_pod_logs", {"name": "web", "tail": 25}, "pod_logs", "", ("web", "default", 25)),
    ("kubernetes_pod_logs", {"name": "web", "namespace": "prod", "tail": 25}, "pod_logs", "ready\n", ("web", "prod", 25)),
    ("kubernetes_list_events", {}, "list_events", [{"name": "web", "reason": "Started", "message": None}], (None,)),
    ("kubernetes_list_events", {"namespace": None}, "list_events", [], (None,)),
    ("kubernetes_list_events", {"namespace": "prod"}, "list_events", [], ("prod",)),
])
def test_kubernetes_delegation_and_results(monkeypatch, name, arguments, method, value, expected_args):
    factory = MagicMock()
    getattr(factory.return_value, method).return_value = value
    monkeypatch.setattr(kubernetes, "KubernetesClient", factory)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments)
            assert not result.is_error
            expected = value if name == "kubernetes_inspect_pod" else {"result": value}
            assert result.structured_content == expected

    run(check())
    factory.assert_called_once_with()
    getattr(factory.return_value, method).assert_called_once_with(*expected_args)
    assert len(factory.return_value.method_calls) == 1


@pytest.mark.parametrize("name,arguments,method,expected_args", [
    ("kubernetes_health", {}, "health_check", ()),
    ("kubernetes_list_namespaces", {}, "list_namespaces", ()),
    ("kubernetes_list_pods", {}, "list_pods", (None,)),
    ("kubernetes_inspect_pod", {"name": "missing"}, "inspect_pod", ("missing", "default")),
    ("kubernetes_pod_logs", {"name": "missing"}, "pod_logs", ("missing", "default", 100)),
    ("kubernetes_list_events", {}, "list_events", (None,)),
])
@pytest.mark.parametrize("construction_error", [False, True])
def test_kubernetes_exceptions_are_tool_errors(monkeypatch, name, arguments, method, expected_args, construction_error):
    factory = MagicMock()
    error = ApiException(status=503, reason="Kubernetes unavailable")
    if construction_error:
        factory.side_effect = error
    else:
        getattr(factory.return_value, method).side_effect = error
    monkeypatch.setattr(kubernetes, "KubernetesClient", factory)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments)
            assert result.is_error
            assert f"Error executing tool {name}" in result.content[0].text

    run(check())
    factory.assert_called_once_with()
    if not construction_error:
        getattr(factory.return_value, method).assert_called_once_with(*expected_args)
        assert len(factory.return_value.method_calls) == 1


def test_docker_mcp_surface_is_exactly_observational():
    async def check():
        async with Client(mcp) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {name for name in names if "docker" in name} == {
                "docker_health", "docker_list_containers", "docker_inspect_container",
                "docker_container_logs", "diagnose_docker_issues",
            }
            for action in ("create", "start", "stop", "restart", "kill", "remove",
                           "exec", "attach", "update", "build"):
                result = await client.call_tool(f"docker_{action}", {})
                assert result.is_error
    run(check())


def test_server_import_and_discovery_do_not_read_backend_settings(monkeypatch):
    import importlib
    from opspilot import configuration, server
    from opspilot.clients import postgres as pg_client, kubernetes as kube_client, docker as docker_client
    forbidden = MagicMock(side_effect=AssertionError("Discovery must not read configuration"))
    for module, name in ((configuration, "load_postgres_config"),
                         (configuration, "load_kubernetes_settings"),
                         (configuration, "load_docker_settings"),
                         (pg_client, "load_postgres_config"),
                         (kube_client, "load_kubernetes_settings"),
                         (docker_client, "load_docker_settings")):
        monkeypatch.setattr(module, name, forbidden)
    importlib.reload(server)

    async def check():
        async with Client(server.mcp) as client:
            assert_tools((await client.list_tools()).tools)
    run(check())
    forbidden.assert_not_called()


def test_fresh_server_import_and_discovery_without_backend_activation():
    import subprocess
    import sys
    import textwrap

    # A separate interpreter ensures server, client and tool modules are fresh.
    # Only this harness starts a subprocess; credential execution inside it fails.
    script = textwrap.dedent('''
        import asyncio
        import socket
        import subprocess
        from unittest.mock import MagicMock
        import docker
        import psycopg
        from kubernetes import client as kube_client, config as kube_config
        from mcp import Client

        forbidden = MagicMock(side_effect=AssertionError("Discovery activated a backend"))
        original_socket = socket.socket
        class OfflineSocket(original_socket):
            def __init__(self, family=socket.AF_INET, *args, **kwargs):
                if family in (socket.AF_INET, socket.AF_INET6):
                    forbidden()
                super().__init__(family, *args, **kwargs)
        # Allow the local socketpair used by asyncio's event-loop wakeup.
        socket.socket = OfflineSocket
        socket.getaddrinfo = forbidden
        socket.create_connection = forbidden
        for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
            setattr(OfflineSocket, name, forbidden)
        subprocess.Popen = forbidden
        psycopg.connect = forbidden
        docker.DockerClient = docker.APIClient = docker.from_env = forbidden
        kube_client.Configuration = kube_client.ApiClient = kube_client.CoreV1Api = forbidden
        kube_config.load_kube_config = kube_config.load_kube_config_from_dict = forbidden

        from opspilot import configuration
        configuration.load_postgres_config = forbidden
        configuration.load_docker_settings = forbidden
        configuration.load_kubernetes_settings = forbidden
        configuration.find_dotenv = forbidden
        configuration._read = forbidden
        from opspilot import server

        async def check():
            async with Client(server.mcp) as client:
                tools = (await client.list_tools()).tools
                assert len(tools) == 17
                assert {"postgres_health", "docker_health", "kubernetes_health",
                        "diagnose_cross_system_issues"} <= {tool.name for tool in tools}
        asyncio.run(check())
        forbidden.assert_not_called()
    ''')
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("tool,arguments", [
    ("docker_container_logs", {"name_or_id": "web", "tail": 1001}),
    ("kubernetes_pod_logs", {"name": "web", "tail": True}),
])
def test_tail_policy_rejected_before_backend_activation(monkeypatch, tool, arguments):
    forbidden = MagicMock(side_effect=AssertionError("Must not activate"))
    monkeypatch.setattr(docker, "DockerClient", forbidden)
    monkeypatch.setattr(kubernetes, "KubernetesClient", forbidden)
    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool(tool, arguments)
            assert result.is_error
    run(check())
    forbidden.assert_not_called()


@pytest.mark.parametrize("failure", ["limit", "timeout"])
def test_safe_observation_errors_on_wire(monkeypatch, failure):
    from opspilot.limits import ObservationLimitError, ObservationTimeoutError
    factory = MagicMock()
    factory.return_value.list_containers.side_effect = (
        ObservationLimitError() if failure == "limit" else ObservationTimeoutError())
    monkeypatch.setattr(docker, "DockerClient", factory)
    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool("docker_list_containers", {})
            assert result.is_error
            expected = "OpsPilot safety limit" if failure == "limit" else "observation timed out"
            assert expected in result.content[0].text
    run(check())


def test_pre_return_observation_limit_on_wire(monkeypatch):
    from opspilot.limits import MAX_VALUE_BYTES
    factory = MagicMock()
    factory.return_value.inspect_container.return_value = {"name": "s" * (MAX_VALUE_BYTES + 1)}
    monkeypatch.setattr(docker, "DockerClient", factory)
    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool("docker_inspect_container", {"name_or_id": "web"})
            assert result.is_error
            assert "OpsPilot safety limit" in result.content[0].text
            assert "ssssssss" not in result.content[0].text
    run(check())
