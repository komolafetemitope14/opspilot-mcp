import asyncio
import importlib
import json
import math
from pathlib import Path
import time
from unittest.mock import MagicMock
from uuid import UUID

from mcp import Client
import pytest

from opspilot import audit, server
from opspilot.tools import diagnostics, docker, kubernetes, postgres
from opspilot.redaction import registry
from tests.test_audit import sink  # shared capturing fixture
from tests.test_mcp import assert_tools


CASES = [
    ("postgres_health", {}, postgres, "PostgresClient", "health_check", False),
    ("postgres_list_tables", {}, postgres, "PostgresClient", "list_tables", ["private-table-result"]),
    ("postgres_query", {"query": "SELECT 'private-sql-input'"}, postgres, "PostgresClient", "execute_select", [{"x": "private-query-result"}]),
    ("docker_health", {}, docker, "DockerClient", "health_check", False),
    ("docker_list_containers", {}, docker, "DockerClient", "list_containers", [{"name": "private-list-result"}]),
    ("docker_inspect_container", {"name_or_id": "web"}, docker, "DockerClient", "inspect_container", {"name": "private-inspect-result"}),
    ("docker_container_logs", {"name_or_id": "web"}, docker, "DockerClient", "container_logs", "private-docker-log"),
    ("kubernetes_health", {}, kubernetes, "KubernetesClient", "health_check", False),
    ("kubernetes_list_namespaces", {}, kubernetes, "KubernetesClient", "list_namespaces", ["private-namespace-result"]),
    ("kubernetes_list_pods", {}, kubernetes, "KubernetesClient", "list_pods", [{"name": "private-pod-result"}]),
    ("kubernetes_inspect_pod", {"name": "web"}, kubernetes, "KubernetesClient", "inspect_pod", {"name": "private-pod-result"}),
    ("kubernetes_pod_logs", {"name": "web"}, kubernetes, "KubernetesClient", "pod_logs", "private-kube-log"),
    ("kubernetes_list_events", {}, kubernetes, "KubernetesClient", "list_events", [{"message": "private-event-message"}]),
    ("diagnose_docker_issues", {"name_or_id": "web"}, diagnostics, "DockerClient", "inspect_container", {}),
    ("diagnose_postgres_issues", {}, diagnostics, "PostgresClient", "health_check", True),
    ("diagnose_kubernetes_issues", {"name": "web"}, diagnostics, "KubernetesClient", "inspect_pod", {}),
    ("diagnose_cross_system_issues", {"name": "web"}, diagnostics, "KubernetesClient", "inspect_pod", {}),
]


@pytest.mark.parametrize("name,args,module,factory,method,value", CASES)
@pytest.mark.parametrize("fails", [False, True])
def test_all_tools(monkeypatch, sink, name, args, module, factory, method, value, fails):
    fake = MagicMock()
    fake.return_value.list_events.return_value = []
    fake.return_value.list_tables.return_value = []
    getattr(fake.return_value, method).return_value = value
    if fails:
        fake.side_effect = RuntimeError("private-raw-exception password=private-credential")
    monkeypatch.setattr(module, factory, fake)
    if module is diagnostics:
        for cls, method_name in (("DockerDiagnostics", "diagnose_container"), ("KubernetesDiagnostics", "diagnose_pod"), ("PostgresDiagnostics", "diagnose_database"), ("CorrelationDiagnostics", "correlate")):
            diagnostic = MagicMock()
            getattr(diagnostic.return_value, method_name).return_value = {"findings": ["private-findings"], "evidence": "private-evidence", "correlation": "private-correlation"}
            monkeypatch.setattr(diagnostics, cls, diagnostic)
        monkeypatch.setattr(diagnostics, "load_kubernetes_settings", lambda: None)
        monkeypatch.setattr(diagnostics, "load_postgres_config", lambda: None)
        if factory != "PostgresClient":
            monkeypatch.setattr(diagnostics, "PostgresClient", MagicMock())
    async def check():
        async with Client(server.mcp) as client:
            result = await client.call_tool(name, args)
            assert result.is_error is fails
            if not fails and module is not diagnostics:
                expected = value if isinstance(value, dict) else {"result": value}
                assert result.structured_content == expected
    asyncio.run(check())
    lines = sink.getvalue().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert "private-" not in lines[0]
    assert event["tool"] == name
    assert event["outcome"] == ("failure" if fails else "success")
    assert (event.get("error_category") == "internal") if fails else ("error_category" not in event)
    assert math.isfinite(event["duration_ms"]) and event["duration_ms"] >= 0
    UUID(event["invocation_id"])
    assert event["timestamp"].endswith("Z")
    assert "tail" not in event
    assert list(event)[:5] == ["schema_version", "timestamp", "event", "invocation_id", "request_id"]


def test_discovery_and_import(monkeypatch, sink):
    forbidden = MagicMock(side_effect=AssertionError("backend activation"))
    for module, name in ((postgres, "PostgresClient"), (docker, "DockerClient"), (kubernetes, "KubernetesClient"), (diagnostics, "PostgresClient"), (diagnostics, "DockerClient"), (diagnostics, "KubernetesClient")):
        monkeypatch.setattr(module, name, forbidden)
    importlib.reload(server)
    async def check():
        async with Client(server.mcp) as client:
            tools = (await client.list_tools()).tools
            assert_tools(tools)
            assert set(audit.TOOL_POLICY) == {t.name for t in tools}
    asyncio.run(check())
    assert sink.getvalue() == ""
    forbidden.assert_not_called()


@pytest.mark.parametrize("name,args", [("private-unknown-tool\n", {"password": "private-password"}), ("postgres_query", {}), ("postgres_query", {"query": 123}), ("docker_container_logs", {"name_or_id": "web", "tail": True})])
def test_rejections(sink, name, args):
    async def check():
        async with Client(server.mcp) as client:
            assert (await client.call_tool(name, args)).is_error
    asyncio.run(check())
    event = json.loads(sink.getvalue())
    assert event["error_category"] == "validation"
    assert event["outcome"] == "failure"
    assert "private-" not in sink.getvalue()
    if name.startswith("private-"):
        assert event["tool"] == "unknown"
        assert "resource" not in event


@pytest.mark.parametrize("error,category", [(audit.ConfigurationError("private-configuration"), "configuration"), (audit.ObservationLimitError(), "limit"), (audit.ObservationTimeoutError(), "timeout"), (audit.ObservationError("safe constant"), "observation")])
def test_real_conversion_category(monkeypatch, sink, error, category):
    fake = MagicMock(side_effect=error)
    monkeypatch.setattr(docker, "DockerClient", fake)
    async def check():
        async with Client(server.mcp) as client:
            assert (await client.call_tool("docker_health", {})).is_error
    asyncio.run(check())
    assert json.loads(sink.getvalue())["error_category"] == category


def test_identifier_secret_and_final_output_redaction(monkeypatch, sink):
    registry.register("audit-resource-secret")
    fake = MagicMock()
    fake.return_value.inspect_container.return_value = {"password": "private-result-password"}
    monkeypatch.setattr(docker, "DockerClient", fake)
    async def check():
        async with Client(server.mcp) as client:
            result = await client.call_tool("docker_inspect_container", {"name_or_id": "web-audit-resource-secret"})
            assert result.structured_content == {"password": "[REDACTED]"}
    asyncio.run(check())
    assert json.loads(sink.getvalue())["resource"] == "web-[REDACTED]"
    assert "private-result-password" not in sink.getvalue()


def test_output_sanitization_failure(monkeypatch, sink):
    fake = MagicMock()
    fake.return_value.health_check.return_value = True
    monkeypatch.setattr(docker, "DockerClient", fake)
    def broken(value):
        raise RuntimeError("private-redaction-error")
    monkeypatch.setattr(audit.redactor, "redact_value", broken)
    async def check():
        async with Client(server.mcp) as client:
            with pytest.raises(Exception):
                await client.call_tool("docker_health", {})
    asyncio.run(check())
    assert json.loads(sink.getvalue())["error_category"] == "internal"
    assert "private-" not in sink.getvalue()


async def stdio_exchange(script, messages, timeout=15):
    """Bound writes, reads, and shutdown together; always reap the child."""
    root = Path(__file__).resolve().parents[1]
    process = await asyncio.create_subprocess_exec(
        str(root / ".venv/bin/python"), "-c", script, cwd=root,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr = asyncio.create_task(process.stderr.read())

    async def exchange():
        responses = []
        for message in messages:
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            if "id" in message:
                responses.append(json.loads(await process.stdout.readline()))
        process.stdin.close()
        trailing = await process.stdout.read()
        await process.wait()
        return responses, trailing, (await asyncio.shield(stderr)).decode(), process.returncode

    try:
        return await asyncio.wait_for(exchange(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        await stderr


def stdio_server_script(expected_health_calls=0):
    # Every backend is mocked, including failed pre-dispatch requests.
    return """
from unittest.mock import MagicMock
from opspilot import server
from opspilot.tools import postgres, docker, kubernetes, diagnostics
from opspilot.audit import configure_audit_stderr
from opspilot.redaction import configure_stderr
factories = []
for module in (postgres, docker, kubernetes, diagnostics):
    for name in ("PostgresClient", "DockerClient", "KubernetesClient"):
        if hasattr(module, name):
            fake = MagicMock(side_effect=AssertionError("backend forbidden"))
            setattr(module, name, fake)
            factories.append(fake)
health = MagicMock()
health.return_value.health_check.return_value = False
docker.DockerClient = health
configure_stderr()
configure_audit_stderr()
server.mcp.run(transport="stdio")
for factory in factories:
    factory.assert_not_called()
assert health.call_count == """ + str(expected_health_calls) + "\n"


def handshake():
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "offline-test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


def test_stdio_audit_stderr_only():
    messages = handshake() + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "postgres_query", "arguments": {}}},
    ]
    responses, trailing, stderr, code = asyncio.run(stdio_exchange(stdio_server_script(), messages))
    assert code == 0 and trailing == b""
    assert [r["id"] for r in responses] == [1, 2]
    assert responses[1]["result"]["isError"]
    events = [json.loads(line) for line in stderr.splitlines() if line.startswith('{')]
    assert len(events) == 1 and events[0]["tool"] == "postgres_query"
    assert events[0]["error_category"] == "validation"
    assert "backend forbidden" not in stderr


@pytest.mark.parametrize("state", [123, "private-unknown-request-state-token"])
def test_request_state_rejections_are_audited(state):
    messages = handshake() + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "docker_health", "arguments": {}, "requestState": state}},
    ]
    responses, trailing, stderr, code = asyncio.run(stdio_exchange(stdio_server_script(), messages))
    assert code == 0 and trailing == b""
    assert responses[1]["error"]["code"] == -32602
    lines = [line for line in stderr.splitlines() if line.startswith('{')]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["outcome"] == "failure" and event["error_category"] == "validation"
    assert event["tool"] == "docker_health"
    assert "requestState" not in lines[0]
    assert state not in event.values()
    if isinstance(state, str):
        assert state not in lines[0]


def test_tool_notification_is_not_audited():
    messages = handshake() + [
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "docker_health", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    responses, trailing, stderr, code = asyncio.run(stdio_exchange(stdio_server_script(), messages))
    assert code == 0 and trailing == b""
    assert responses[1]["id"] == 2
    assert not any(line.startswith('{') for line in stderr.splitlines())


@pytest.mark.parametrize("request_id", ["safe-id", "unsafe-" + "x" * 200])
def test_raw_request_id_distinguishes_requests(request_id):
    messages = handshake() + [
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": "docker_health", "arguments": {}}},
    ]
    responses, trailing, stderr, code = asyncio.run(stdio_exchange(stdio_server_script(1), messages))
    assert code == 0 and trailing == b""
    assert responses[1]["id"] == request_id
    assert not responses[1]["result"].get("isError", False)
    assert responses[1]["result"]["structuredContent"] == {"result": False}
    events = [json.loads(line) for line in stderr.splitlines() if line.startswith('{')]
    assert len(events) == 1 and events[0]["outcome"] == "success"
    if request_id == "safe-id":
        assert events[0]["request_id"] == request_id
    else:
        assert "request_id" not in events[0] and request_id not in stderr


def test_stdio_nonresponsive_child_is_killed_and_reaped(monkeypatch):
    children = []
    create = asyncio.create_subprocess_exec
    async def track(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(stdio_exchange("import time; time.sleep(60)", handshake(), timeout=0.2))
    assert time.monotonic() - start < 5
    assert len(children) == 1 and children[0].returncode is not None


def test_concurrent_mcp_invocations(monkeypatch, sink):
    fake = MagicMock()
    def inspect(name):
        if name == "limited":
            raise audit.ObservationLimitError()
        return {"name": name}
    fake.return_value.inspect_container.side_effect = inspect
    monkeypatch.setattr(docker, "DockerClient", fake)
    async def check():
        async with Client(server.mcp) as client:
            results = await asyncio.gather(*(client.call_tool("docker_inspect_container", {"name_or_id": name}) for name in ("limited", "healthy", "other")))
            assert [r.is_error for r in results] == [True, False, False]
    asyncio.run(check())
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert len(events) == 3
    assert len({e["request_id"] for e in events}) == 3
    assert len({e["invocation_id"] for e in events}) == 3
    by_target = {e["resource"]: e for e in events}
    assert by_target["limited"]["error_category"] == "limit"
    for name in ("healthy", "other"):
        assert by_target[name]["outcome"] == "success"
        assert "error_category" not in by_target[name]


def test_audit_is_outermost_without_removing_sdk_middleware():
    names = [type(middleware).__name__ for middleware in server.mcp.middleware]
    assert names == ["AuditMiddleware", "OpenTelemetryMiddleware",
                     "RequestStateBoundary", "RedactionMiddleware"]
