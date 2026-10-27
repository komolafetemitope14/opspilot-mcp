"""Exercise runtime startup with synthetic credentials and forbidden backends."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_mcp_audit import handshake, stdio_exchange


ROOT = Path(__file__).resolve().parents[1]
SECRET = "startup-private-secret-DO-NOT-USE"

# Block real activation in the child, including Docker's version negotiation.
# Local socketpair creation remains available to asyncio.
OFFLINE = """
import socket
import subprocess
from unittest.mock import MagicMock
import docker as docker_sdk
import psycopg
from kubernetes import client as kube_client

forbidden = MagicMock(side_effect=AssertionError("Infrastructure forbidden"))
for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
    setattr(socket.socket, name, forbidden)
socket.getaddrinfo = socket.create_connection = forbidden
subprocess.Popen = forbidden
psycopg.connect = forbidden
docker_sdk.DockerClient = docker_sdk.APIClient = docker_sdk.from_env = forbidden
kube_client.ApiClient = kube_client.CoreV1Api = forbidden

from opspilot import configuration
configuration.load_postgres_config = forbidden
configuration.load_docker_settings = forbidden
configuration.load_kubernetes_settings = forbidden
configuration.find_dotenv = configuration._read = forbidden
"""


def startup(path):
    if path == "module":
        return 'import runpy\nrunpy.run_module("opspilot.server", run_name="__main__")\n'
    # Actual installed CLI command function: imports the object, then calls run.
    return ('from mcp.cli.cli import run\n'
            'run("src/opspilot/server.py:mcp", transport="stdio")\n')


@pytest.mark.parametrize("path", ["module", "cli"])
def test_startup_installs_owned_handlers_idempotently(path):
    script = OFFLINE + """
import json
import logging
from mcp.server import MCPServer
from opspilot import audit
from opspilot.redaction import SafeStderrHandler, RedactingFormatter

calls = []
def intercepted(self, transport="stdio", **kwargs):
    assert transport == "stdio" and kwargs == {}
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert type(root.handlers[0]) is SafeStderrHandler
    assert type(root.handlers[0].formatter) is RedactingFormatter
    assert root.level == logging.INFO
    assert len(audit.logger.handlers) == 1
    assert type(audit.logger.handlers[0]) is SafeStderrHandler
    assert type(audit.logger.handlers[0].formatter) is audit.AuditFormatter
    assert audit.logger.level == logging.INFO
    assert not audit.logger.propagate
    calls.append(transport)

MCPServer.run = intercepted
""" + startup(path) + startup(path) + """
assert calls == ["stdio", "stdio"]
forbidden.assert_not_called()
audit.logger.info({"event": "startup_probe"})
"""
    # subprocess.run kills and reaps the child if communicate exceeds timeout.
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert [json.loads(line) for line in result.stderr.splitlines()] == [
        {"event": "startup_probe"},
    ]


@pytest.mark.parametrize("path", ["module", "cli"])
def test_startup_framework_failure_is_redacted_and_audited(path):
    script = OFFLINE + f"""
from mcp.server import MCPServer
from opspilot.redaction import registry
from opspilot.tools import docker

registry.register({SECRET!r})
failure = MagicMock(side_effect=RuntimeError(
    {SECRET!r} + " password=startup-recognized-secret private-exception-text"))
docker.DockerClient = failure

# Configure through the runtime boundary twice, then serve one real request.
# The first run stops at the inherited runner; no configure helper is called here.
inherited_run = MCPServer.run
MCPServer.run = lambda self, *args, **kwargs: None
""" + startup(path) + "\nMCPServer.run = inherited_run\n" + startup(path) + """
failure.assert_called_once_with()
forbidden.assert_not_called()
"""
    messages = handshake() + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "docker_health", "arguments": {}}},
    ]
    # Shared bounded harness kills/reaps the child in finally, including timeout.
    responses, trailing, stderr, code = asyncio.run(
        stdio_exchange(script, messages, timeout=20))
    assert code == 0 and trailing == b""
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert all(response["jsonrpc"] == "2.0" for response in responses)
    assert "protocolVersion" in responses[0]["result"]
    assert len(responses[1]["result"]["tools"]) == 17
    result = responses[2]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Error executing tool docker_health"
    wire = json.dumps(responses)
    for secret in (SECRET, "startup-recognized-secret"):
        assert secret not in wire + stderr
    assert "RuntimeError" in stderr and "[REDACTED]" in stderr
    events = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "tool_invocation"
    assert event["tool"] == "docker_health"
    assert event["outcome"] == "failure"
    assert event["error_category"] == "internal"
    assert set(event) == {
        "schema_version", "timestamp", "event", "invocation_id", "request_id",
        "tool", "backend", "action", "resource_type", "outcome", "duration_ms",
        "error_category",
    }
    assert "private-exception-text" not in json.dumps(event)


def test_import_and_discovery_do_not_configure_owned_logging():
    script = OFFLINE + """
import asyncio
import logging
from mcp import Client
from opspilot import audit, redaction

# SDK basicConfig must also leave a pre-existing embedding handler alone.
root = logging.getLogger()
sentinel = logging.NullHandler()
root.handlers[:] = [sentinel]
root.setLevel(logging.WARNING)
assert audit.logger.handlers == []
redaction.configure_stderr = forbidden
audit.configure_audit_stderr = forbidden

from opspilot import server

async def check():
    async with asyncio.timeout(10):
        async with Client(server.mcp) as client:
            assert len((await client.list_tools()).tools) == 17

asyncio.run(check())
assert root.handlers == [sentinel]
assert root.level == logging.WARNING
assert audit.logger.handlers == []
assert not audit.logger.propagate
assert callable(server.mcp.run_sse_async)
assert callable(server.mcp.run_streamable_http_async)
forbidden.assert_not_called()
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == result.stderr == ""
