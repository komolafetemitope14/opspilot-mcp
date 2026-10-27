import asyncio
import io
import json
import logging
import math
from types import SimpleNamespace
from uuid import UUID

import docker
from kubernetes.client.exceptions import ApiException
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
import psycopg
import pytest
from requests.exceptions import Timeout

from opspilot import audit
from opspilot.configuration import ConfigurationError
from opspilot.limits import ObservationError, ObservationLimitError, ObservationTimeoutError, ObservationToolError
from opspilot.redaction import Redactor, SecretRegistry, SafeStderrHandler


@pytest.fixture
def sink():
    stream = io.StringIO()
    handler = SafeStderrHandler(stream)
    handler.setFormatter(audit.AuditFormatter())
    old = audit.logger.handlers[:]
    audit.logger.handlers[:] = [handler]
    yield stream
    audit.logger.handlers[:] = old


def context(params=None, request_id=1):
    return SimpleNamespace(method="tools/call", params=params or {"name": "docker_health"}, request_id=request_id)


@pytest.mark.parametrize("value", [True, None, {}, [], 2**64, -(2**64), "x"*129, "", "a\nb", "a\rb", "a\tb", "a\0b", "a\x1bb", "a\u2028b"])
def test_request_ids_rejected(value):
    assert audit.request_id(value) is None


@pytest.mark.parametrize("value", [0, -1, 2**63-1, 'id"\\'])
def test_request_ids_allowed(value):
    assert audit.request_id(value) == value


@pytest.mark.parametrize("value", [None, 1, {}, "", "x"*256, "a\nb", "a\rb", "a\tb", "a\0b", "a\x1bb", "a\u2028b", 'a"b', 'a\\b', "/web", "a/b"])
def test_identifier_injection_omitted(value):
    for kwargs in ({}, {"namespace": True}, {"docker_name": True}):
        assert audit.identifier(value, **kwargs) is None


@pytest.mark.parametrize("value", ["a..b", ".a", "a.", "-a", "a-", "A", "a_", "a"*64 + ".b"])
def test_invalid_kubernetes_labels(value):
    assert audit.identifier(value) is None


def test_target_semantics():
    assert audit.target_metadata({"name": "kubernetes_list_pods"})["scope"] == "all_namespaces"
    assert audit.target_metadata({"name": "kubernetes_list_events", "arguments": {"namespace": None}})["scope"] == "all_namespaces"
    assert audit.target_metadata({"name": "kubernetes_inspect_pod", "arguments": {"name": "a.b"}})["namespace"] == "default"
    assert "namespace" not in audit.target_metadata({"name": "kubernetes_inspect_pod", "arguments": {"namespace": None}})
    assert audit.identifier("a.b") == "a.b"
    assert audit.identifier("a.b", namespace=True) is None
    assert audit.identifier("Web_1", docker_name=True) == "Web_1"


@pytest.mark.parametrize("error,category", [
    (ConfigurationError("secret"), "configuration"), (ObservationLimitError(), "limit"),
    (ObservationTimeoutError(), "timeout"), (ObservationError("secret"), "observation"),
    (ObservationToolError(ObservationLimitError()), "limit"),
    (ObservationToolError(ObservationTimeoutError()), "timeout"),
    (ObservationToolError(ObservationError("secret")), "observation"),
    (ToolError("secret"), "tool"), (RuntimeError("secret"), "internal"),
    (UnexpectedToolError("secret"), "internal"), (MCPError(-32602, "secret"), "validation"),
    (MCPError(-32603, "secret"), "internal"), (asyncio.CancelledError(), "cancelled"),
    (psycopg.errors.InsufficientPrivilege("secret"), "authorization"),
    (psycopg.errors.InvalidPassword("secret"), "authorization"),
    (psycopg.errors.SyntaxError("secret"), "backend"),
    (psycopg.errors.ConnectionTimeout("secret"), "timeout"),
    (ApiException(status=403, reason="secret"), "authorization"),
    (ApiException(status=503, reason="secret"), "backend"),
    (docker.errors.DockerException("secret"), "backend"), (Timeout("secret"), "timeout"),
])
def test_categories(error, category):
    wrapped = UnexpectedToolError("outer")
    wrapped.__cause__ = error
    assert audit.classify_error(wrapped) == category


def test_cause_cycles_and_bound():
    error = ToolError("secret")
    error.__cause__ = error
    assert audit.classify_error(error) == "tool"
    for _ in range(20):
        outer = RuntimeError()
        outer.__cause__ = error
        error = outer
    assert audit.classify_error(error) == "internal"


def test_json_redacts_values_before_encoding():
    registry = SecretRegistry()
    for value in ('quote"slash\\\nsecret', "private"):
        registry.register(value)
    formatter = audit.AuditFormatter(Redactor(registry))
    record = logging.LogRecord("opspilot.audit", 20, "", 0, {"private": "private", "value": 'quote"slash\\\nsecret\u2028', "number": 1}, (), None)
    line = formatter.format(record)
    assert len(line.splitlines()) == 1
    assert json.loads(line) == {"private": "[REDACTED]", "value": "[REDACTED]\u2028", "number": 1}
    record.msg = {"duration_ms": float("nan")}
    with pytest.raises(ValueError):
        formatter.format(record)


@pytest.mark.parametrize("failure", ["metadata", "formatter", "serializer", "redactor", "logger", "filter", "write", "flush"])
@pytest.mark.parametrize("tool_fails", [False, True])
def test_fail_open(monkeypatch, sink, failure, tool_fails):
    def broken(*args, **kwargs):
        raise RuntimeError("private-failure")
    handler = audit.logger.handlers[0]
    if failure == "metadata":
        monkeypatch.setattr(audit, "target_metadata", broken)
    elif failure == "formatter":
        monkeypatch.setattr(handler.formatter, "format", broken)
    elif failure == "serializer":
        monkeypatch.setattr(audit.json, "dumps", broken)
    elif failure == "redactor":
        monkeypatch.setattr(handler.formatter.policy, "redact_text", broken)
    elif failure == "logger":
        monkeypatch.setattr(audit.logger, "info", broken)
    elif failure == "filter":
        monkeypatch.setattr(handler, "filter", broken)
    else:
        monkeypatch.setattr(sink, failure, broken)
    original = ValueError("original-private-error")
    async def next_call(ctx):
        if tool_fails:
            raise original
        return {"isError": False, "payload": "private-result"}
    async def check():
        if tool_fails:
            with pytest.raises(ValueError) as caught:
                await audit.AuditMiddleware()(context(), next_call)
            assert caught.value is original
        else:
            assert await audit.AuditMiddleware()(context(), next_call) == {"isError": False, "payload": "private-result"}
        assert audit._state.get() is None
    asyncio.run(check())


def test_concurrent_context_and_cancellation(sink):
    async def run_one(index):
        async def next_call(ctx):
            await asyncio.sleep(0)
            audit._state.get()["category"] = "limit" if index % 2 else "timeout"
            return {"isError": True}
        await audit.AuditMiddleware()(context({"name": "docker_inspect_container", "arguments": {"name_or_id": f"web{index}"}}, index), next_call)
    async def check():
        await asyncio.gather(*(run_one(i) for i in range(20)))
        original = asyncio.CancelledError()
        async def cancelled(ctx):
            raise original
        with pytest.raises(asyncio.CancelledError) as caught:
            await audit.AuditMiddleware()(context(request_id=100), cancelled)
        assert caught.value is original
    asyncio.run(check())
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert len(events) == 21
    assert len({UUID(e["invocation_id"]) for e in events}) == 21
    for e in events[:20]:
        i = e["request_id"]
        assert e["resource"] == f"web{i}"
        assert e["error_category"] == ("limit" if i % 2 else "timeout")
        assert math.isfinite(e["duration_ms"]) and e["duration_ms"] >= 0
    assert events[-1]["error_category"] == "cancelled"


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\tb", "a\0b", "a\x1bb", "a\u2028b", 'a"b', 'a\\b', "a"*10000, {}, True])
def test_malicious_metadata_one_line(sink, value):
    async def next_call(ctx):
        return {"isError": False}
    for name, args in (("docker_inspect_container", {"name_or_id": value}), ("kubernetes_inspect_pod", {"name": value, "namespace": value}), ("private-unknown", {"password": value})):
        asyncio.run(audit.AuditMiddleware()(context({"name": name, "arguments": args}, value), next_call))
    lines = sink.getvalue().splitlines()
    assert len(lines) == 3
    for line in lines:
        event = json.loads(line)
        assert "namespace" not in event and "resource" not in event
        if audit.request_id(value) is None:
            assert "request_id" not in event
        assert "private-unknown" not in line


def test_docker_http_authorization():
    from requests import Response
    response = Response()
    response.status_code = 401
    assert audit.classify_error(docker.errors.APIError("private", response=response)) == "authorization"
    response.status_code = 500
    assert audit.classify_error(docker.errors.APIError("forbidden", response=response)) == "backend"


def test_configure_audit_stderr_idempotent(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    old = audit.logger.handlers[:]
    try:
        audit.configure_audit_stderr()
        audit.configure_audit_stderr()
        assert len(audit.logger.handlers) == 1
        assert audit.logger.level == logging.INFO and not audit.logger.propagate
        audit.logger.info({"event": "test", "number": 1})
        assert json.loads(stream.getvalue()) == {"event": "test", "number": 1}
    finally:
        audit.logger.handlers[:] = old


def test_classification_bookkeeping_failure_preserves_exception(monkeypatch):
    class BrokenState:
        def __setitem__(self, key, value):
            raise RuntimeError("private-bookkeeping")
    original = RuntimeError("private-original")
    async def broken(*args, **kwargs):
        raise original
    monkeypatch.setattr(audit.MCPServer, "call_tool", broken)
    async def check():
        token = audit._state.set(BrokenState())
        try:
            with pytest.raises(RuntimeError) as caught:
                await audit.AuditedMCPServer("test").call_tool("docker_health", {})
            assert caught.value is original
        finally:
            audit._state.reset(token)
    asyncio.run(check())


def test_audit_handler_drops_formatter_failure_without_diagnostics(monkeypatch, capsys):
    stream = io.StringIO()
    handler = SafeStderrHandler(stream)
    handler.setFormatter(audit.AuditFormatter())
    monkeypatch.setattr(logging, "raiseExceptions", True)
    record = logging.LogRecord("opspilot.audit", 20, "", 0, {"private": object()}, (), None)
    handler.handle(record)
    assert stream.getvalue() == ""
    assert capsys.readouterr().err == ""
