"""Best-effort metadata-only audit of completed MCP tool requests.

This is not durable storage or a diagnostic logger. Never supply observations,
SQL, exception text, configuration, or arbitrary request metadata to this logger.
"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import re
import time
from uuid import uuid4

import docker
from kubernetes.client.exceptions import ApiException
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
import psycopg
from pydantic import ValidationError
from requests.exceptions import Timeout
from urllib3.exceptions import TimeoutError as HTTPTimeoutError

from opspilot.configuration import ConfigurationError
from opspilot.limits import (
    ObservationError, ObservationLimitError, ObservationTimeoutError,
    ObservationToolError,
)
from opspilot.redaction import SafeStderrHandler, redactor


@dataclass(frozen=True)
class ToolPolicy:
    backend: str
    action: str
    resource_type: str
    resource_arg: str | None = None
    namespace: str | None = None  # "all" or "default" namespace semantics


TOOL_POLICY = {
    "postgres_health": ToolPolicy("postgres", "health", "database"),
    "postgres_list_tables": ToolPolicy("postgres", "list", "table"),
    "postgres_query": ToolPolicy("postgres", "query", "database"),
    "docker_health": ToolPolicy("docker", "health", "engine"),
    "docker_list_containers": ToolPolicy("docker", "list", "container"),
    "docker_inspect_container": ToolPolicy("docker", "inspect", "container", "name_or_id"),
    "docker_container_logs": ToolPolicy("docker", "logs", "container", "name_or_id"),
    "kubernetes_health": ToolPolicy("kubernetes", "health", "cluster"),
    "kubernetes_list_namespaces": ToolPolicy("kubernetes", "list", "namespace"),
    "kubernetes_list_pods": ToolPolicy("kubernetes", "list", "pod", namespace="all"),
    "kubernetes_inspect_pod": ToolPolicy("kubernetes", "inspect", "pod", "name", "default"),
    "kubernetes_pod_logs": ToolPolicy("kubernetes", "logs", "pod", "name", "default"),
    "kubernetes_list_events": ToolPolicy("kubernetes", "list", "event", namespace="all"),
    "diagnose_docker_issues": ToolPolicy("docker", "diagnose", "container", "name_or_id"),
    "diagnose_postgres_issues": ToolPolicy("postgres", "diagnose", "database"),
    "diagnose_kubernetes_issues": ToolPolicy("kubernetes", "diagnose", "pod", "name", "default"),
    "diagnose_cross_system_issues": ToolPolicy("kubernetes_postgres", "correlate", "pod", "name", "default"),
}

_CATEGORIES = frozenset({"configuration", "validation", "timeout", "limit", "authorization",
                         "backend", "observation", "tool", "internal", "cancelled"})
_DOCKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", re.ASCII)
_REQUEST = re.compile(r"[\x20-\x7e]{1,128}", re.ASCII)
_state = ContextVar("opspilot_audit", default=None)
logger = logging.getLogger("opspilot.audit")
logger.setLevel(logging.INFO)
logger.propagate = False


def request_id(value):
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        return value
    if type(value) is str and _REQUEST.fullmatch(value):
        return value
    return None


def identifier(value, *, docker_name=False, namespace=False):
    if type(value) is not str:
        return None
    maximum = 255 if docker_name else 63 if namespace else 253
    if not 1 <= len(value) <= maximum:
        return None
    if docker_name:
        return value if _DOCKER.fullmatch(value) else None
    labels = value.split(".")
    if namespace and len(labels) != 1:
        return None
    return value if all(len(label) <= 63 and _LABEL.fullmatch(label) for label in labels) else None


def target_metadata(params):
    """Read only known target fields; never copy the arguments mapping."""
    name = params.get("name") if type(params) is dict else None
    policy = TOOL_POLICY.get(name) if type(name) is str else None
    if policy is None:
        return {"tool": "unknown", "backend": "unknown", "action": "unknown", "resource_type": "unknown"}
    event = {"tool": name, "backend": policy.backend, "action": policy.action,
             "resource_type": policy.resource_type}
    args = params.get("arguments")
    args = args if type(args) is dict else {}
    if policy.namespace:
        value = args.get("namespace", None if policy.namespace == "all" else "default")
        if value is None and policy.namespace == "all":
            event["scope"] = "all_namespaces"
        else:
            normalized = identifier(value, namespace=True)
            if normalized is not None:
                event.update(scope="namespace", namespace=normalized)
    if policy.resource_arg:
        normalized = identifier(args.get(policy.resource_arg), docker_name=policy.backend == "docker")
        if normalized is not None:
            event["resource"] = normalized
    return event


def classify_error(error):
    """Inspect types and safe status fields, never messages or suppressed context."""
    seen = set()
    fallback = "internal"
    for _ in range(12):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        if isinstance(error, asyncio.CancelledError):
            return "cancelled"
        if isinstance(error, ConfigurationError):
            return "configuration"
        if isinstance(error, ObservationToolError):
            return error.audit_category if error.audit_category in _CATEGORIES else "observation"
        if isinstance(error, ObservationTimeoutError):
            return "timeout"
        if isinstance(error, ObservationLimitError):
            return "limit"
        if isinstance(error, ObservationError):
            return "observation"
        if isinstance(error, ValidationError):
            return "validation"
        if isinstance(error, MCPError):
            return "validation" if error.code in {-32600, -32601, -32602} else "internal"
        if isinstance(error, (Timeout, HTTPTimeoutError, psycopg.errors.ConnectionTimeout,
                              psycopg.errors.QueryCanceled)):
            return "timeout"
        if isinstance(error, psycopg.Error):
            code = error.sqlstate
            return "authorization" if code == "42501" or (type(code) is str and code.startswith("28")) else "backend"
        if isinstance(error, ApiException):
            return "authorization" if type(error.status) is int and error.status in {401, 403} else "backend"
        if isinstance(error, docker.errors.DockerException):
            status = error.response.status_code if isinstance(error, docker.errors.APIError) and error.response is not None else None
            return "authorization" if type(status) is int and status in {401, 403} else "backend"
        if isinstance(error, ToolError) and not isinstance(error, UnexpectedToolError):
            fallback = "tool"
        error = error.__cause__
    return fallback


class AuditedMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except BaseException as exc:
            try:
                state = _state.get()
                if state is not None:
                    state["category"] = "validation" if name not in TOOL_POLICY else classify_error(exc)
            except Exception:
                pass
            raise


class AuditFormatter(logging.Formatter):
    def __init__(self, policy=redactor):
        super().__init__()
        self.policy = policy

    def format(self, record):
        # Structural keys are owned by OpsPilot; only values are redacted.
        event = {}
        for key, value in record.msg.items():
            if type(value) is str:
                value = self.policy.redact_text(value)
            elif type(value) not in (int, float):
                raise TypeError("Invalid audit primitive")
            if type(value) is float and not math.isfinite(value):
                raise ValueError("Invalid audit number")
            event[key] = value
        return json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def configure_audit_stderr():
    handler = SafeStderrHandler()
    handler.setFormatter(AuditFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


class AuditMiddleware:
    async def __call__(self, ctx, call_next):
        if ctx.method != "tools/call" or ctx.request_id is None:
            return await call_next(ctx)
        token = None
        state = {}
        metadata = None
        invocation = None
        start = None
        outcome = "failure"
        category = "internal"
        try:
            # Audit setup failure must never prevent dispatch.
            try:
                token = _state.set(state)
                invocation = str(uuid4())
                start = time.monotonic()
                metadata = target_metadata(ctx.params)
                safe_id = request_id(ctx.request_id)
            except Exception:
                metadata = None
            try:
                result = await call_next(ctx)
                try:
                    failed = result.get("isError", False) if isinstance(result, dict) else getattr(result, "is_error", False)
                    outcome = "failure" if failed else "success"
                    category = state.get("category", "tool")
                except Exception:
                    metadata = None
                return result
            except BaseException as exc:
                try:
                    category = classify_error(exc)
                except Exception:
                    category = "internal"
                raise
        finally:
            try:
                if metadata is not None:
                    duration = max(0.0, (time.monotonic() - start) * 1000)
                    if not math.isfinite(duration):
                        raise ValueError("Invalid duration")
                    event = {"schema_version": 1, "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                             "event": "tool_invocation", "invocation_id": invocation}
                    if safe_id is not None:
                        event["request_id"] = safe_id
                    event.update(metadata)
                    event.update(outcome=outcome, duration_ms=duration)
                    if outcome == "failure":
                        event["error_category"] = category if category in _CATEGORIES else "internal"
                    logger.info(event)
            except Exception:
                pass
            finally:
                if token is not None:
                    try:
                        _state.reset(token)
                    except Exception:
                        pass
