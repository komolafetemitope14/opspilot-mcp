"""Exercise tool discovery and calls through the MCP v2 client."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import docker as docker_sdk
from mcp import Client
from mcp.client.stdio import StdioServerParameters
import psycopg
import pytest

from opspilot.server import mcp
from opspilot.tools import docker, postgres


def run(coroutine):
    async def bounded():
        async with asyncio.timeout(15):
            return await coroutine

    return asyncio.run(bounded())


def assert_tools(tools):
    by_name = {tool.name: tool for tool in tools}
    assert len(tools) == 7
    assert set(by_name) == {
        "postgres_health", "postgres_list_tables", "postgres_query",
        "docker_health", "docker_list_containers", "docker_inspect_container",
        "docker_container_logs",
    }
    for name in ("postgres_health", "postgres_list_tables", "docker_health", "docker_list_containers"):
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


def test_discovery_without_database_configuration(monkeypatch):
    factory = MagicMock(side_effect=AssertionError("Discovery must not connect"))
    monkeypatch.setattr(postgres, "PostgresClient", factory)
    monkeypatch.setattr(docker, "DockerClient", factory)

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
