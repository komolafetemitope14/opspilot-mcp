import asyncio
import copy
import json
from unittest.mock import MagicMock

import pytest
from mcp import Client

from opspilot.server import mcp
from opspilot.tools import postgres, docker, kubernetes, diagnostics
from opspilot.redaction import REDACTED


@pytest.mark.parametrize("module,factory,tool,args,method,value", [
    (postgres, "PostgresClient", "postgres_query", {"query": "SELECT * FROM items"}, "execute_select",
     [{"password": "row-secret", "nested": {"api_key": "row-secret"}, "token_count": 4}]),
    (docker, "DockerClient", "docker_container_logs", {"name_or_id": "web"}, "container_logs",
     "password=log-secret status=503"),
    (kubernetes, "KubernetesClient", "kubernetes_pod_logs", {"name": "web"}, "pod_logs",
     "Bearer log-secret status=503"),
    (kubernetes, "KubernetesClient", "kubernetes_list_events", {}, "list_events",
     [{"message": "token=event-secret", "reason": "Failed"}]),
])
def test_successful_wire_output(module, factory, tool, args, method, value, monkeypatch):
    fake = MagicMock()
    getattr(fake.return_value, method).return_value = value
    monkeypatch.setattr(module, factory, fake)
    before = copy.deepcopy(value)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool(tool, args)
            assert not result.is_error
            wire = json.dumps(result.model_dump(mode="json"))
            for secret in ("row-secret", "log-secret", "event-secret"):
                assert secret not in wire
            assert REDACTED in wire
            if tool == "postgres_query":
                assert result.structured_content["result"][0]["token_count"] == 4
    asyncio.run(check())
    assert value == before


def test_diagnostics_use_raw_events_then_publish_safe_evidence(monkeypatch):
    fake = MagicMock()
    fake.return_value.inspect_pod.return_value = {
        "name": "web", "namespace": "default", "phase": "Running", "ready": True, "restarts": 0,
    }
    events = [{"name": "web", "kind": "Pod", "type": "Warning", "reason": "Failed",
               "message": "ImagePullBackOff token=event-secret"}]
    fake.return_value.list_events.return_value = events
    monkeypatch.setattr(diagnostics, "KubernetesClient", fake)
    original = diagnostics.KubernetesDiagnostics.diagnose_pod
    observed = []

    def inspect_raw(self, pod, received):
        observed.append(copy.deepcopy(received))
        return original(self, pod, received)
    monkeypatch.setattr(diagnostics.KubernetesDiagnostics, "diagnose_pod", inspect_raw)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool("diagnose_kubernetes_issues", {"name": "web"})
            assert not result.is_error
            wire = json.dumps(result.model_dump(mode="json"))
            assert "event-secret" not in wire
            assert "ImagePullBackOff" in wire and REDACTED in wire
    asyncio.run(check())
    assert observed == [events]
    assert "event-secret" in events[0]["message"]


def test_unexpected_errors_remain_generic(monkeypatch):
    fake = MagicMock(side_effect=RuntimeError("password=exception-secret"))
    monkeypatch.setattr(docker, "DockerClient", fake)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool("docker_health", {})
            assert result.is_error
            assert result.content[0].text == "Error executing tool docker_health"
    asyncio.run(check())


def test_server_stderr_sanitizes_framework_traceback(monkeypatch, capsys):
    import logging
    from opspilot.redaction import configure_stderr, registry
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    registry.register("bare-framework-secret")
    fake = MagicMock(side_effect=RuntimeError("bare-framework-secret password=hidden"))
    monkeypatch.setattr(docker, "DockerClient", fake)

    async def check():
        async with Client(mcp) as client:
            result = await client.call_tool("docker_health", {})
            assert result.is_error
    try:
        configure_stderr()
        asyncio.run(check())
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
    output = capsys.readouterr().err
    assert "RuntimeError" in output and REDACTED in output
    assert "bare-framework-secret" not in output
    assert "hidden" not in output


def test_publication_failure_is_safe_on_wire():
    from mcp.server import MCPServer
    from opspilot.redaction import RedactionMiddleware
    class Broken:
        def redact_value(self, value):
            raise ValueError("raw-secret")
    server = MCPServer("fake", middleware=[RedactionMiddleware(Broken())])
    @server.tool()
    async def fake_observation() -> dict:
        return {"message": "raw-secret"}

    async def check():
        async with Client(server) as client:
            with pytest.raises(Exception) as caught:
                await client.call_tool("fake_observation", {})
            assert "Output sanitization failed" in str(caught.value)
            assert "raw-secret" not in str(caught.value)
    asyncio.run(check())
