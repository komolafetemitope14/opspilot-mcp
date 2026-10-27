import asyncio
import copy
import logging
from types import SimpleNamespace

import pytest

from mcp.shared.exceptions import MCPError
from opspilot.redaction import (
    REDACTED, Redactor, SecretRegistry, RedactingFormatter,
    RedactionMiddleware, register_kubernetes_credentials,
)


@pytest.mark.parametrize("text,secret", [
    ("password=hunter2 status=failed", "hunter2"),
    ("passwd='two words' port=5432", "two words"),
    ('token="two words" ready=true', "two words"),
    ("API_KEY=abcdef status=failed", "abcdef"),
    ("api-key: abcdef", "abcdef"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("Authorization: Bearer abcdef", "abcdef"),
    ("failed with Bearer abcdef", "abcdef"),
    ("postgresql://alice:abc123@db:5432/app?sslmode=require", "abc123"),
    ("https://alice:abc123@example.test/path", "alice"),
    ("https://example.test/path?access_token=abc123&limit=20", "abc123"),
    ("https://example.test/?api%5Fkey=abc123", "abc123"),
    ("host=db password='two words' dbname=app", "two words"),
    ("-----BEGIN RSA PRIVATE KEY-----\nabc123\n-----END RSA PRIVATE KEY-----", "abc123"),
    ("-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----", "abc123"),
])
def test_text_forms(text, secret):
    policy = Redactor()
    result = policy.redact_text(text)
    assert secret not in result
    assert REDACTED in result
    assert policy.redact_text(result) == result


@pytest.mark.parametrize("key", [
    "password", "PASSWD", "Token", "api_key", "API-KEY", "apikey",
    "client secret", "AUTHORIZATION", "access_token", "refresh-token", "private_key",
])
def test_credential_keys(key):
    assert Redactor().redact_value({key: {"nested": "sensitive"}}) == {key: REDACTED}


def test_nested_copy_and_operational_values():
    original = {
        "rows": [{"password": "secret", "items": ({"token": 123}, "token=hidden")}],
        "port": 5432, "ready": True, "health": None, "token_count": 4,
        "password_enabled": False, "name": "web", "ip": "10.0.0.1",
    }
    before = copy.deepcopy(original)
    policy = Redactor()
    result = policy.redact_value(original)
    assert original == before
    assert result["rows"][0]["password"] == REDACTED
    assert result["rows"][0]["items"] == ({"token": REDACTED}, "token=" + REDACTED)
    for key in set(original) - {"rows"}:
        assert result[key] == original[key]
    assert policy.redact_value(result) == result


def test_bare_values_short_values_and_environment_selection():
    registry = SecretRegistry()
    registry.register_environment({"POSTGRES_PASSWORD": "bare-secret", "HOST": "db"})
    registry.register("x")
    policy = Redactor(registry)
    assert policy.redact_text("bare-secret db x") == "[REDACTED] db [REDACTED]"
    assert policy.redact_text("[REDACTED]") == REDACTED
    assert registry.values() == ["bare-secret", "x"]


def test_url_preserves_context():
    assert Redactor().redact_text(
        "postgresql://alice:secret@db:5432/app?token=secret&limit=20"
    ) == "postgresql://[REDACTED]@db:5432/app?token=[REDACTED]&limit=20"


def test_formatted_exception_chain_is_sanitized():
    registry = SecretRegistry()
    registry.register("unlabelled-secret")
    try:
        try:
            raise ValueError("unlabelled-secret")
        except ValueError as exc:
            raise RuntimeError("password=hidden status=503") from exc
    except RuntimeError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "operation failed", (), sys.exc_info())
    result = RedactingFormatter(Redactor(registry)).format(record)
    assert "unlabelled-secret" not in result
    assert "hidden" not in result
    assert "RuntimeError" in result and "status=503" in result


def test_fail_closed_formatter_and_boundary():
    class Broken:
        def redact_text(self, value):
            raise RuntimeError("raw-secret")
        redact_value = redact_text
    assert RedactingFormatter(Broken()).format(
        logging.LogRecord("test", logging.ERROR, "", 1, "raw-secret", (), None)
    ) == "Output sanitization failed"

    async def check():
        async def next_call(ctx):
            return {"content": "raw-secret"}
        with pytest.raises(MCPError, match="Output sanitization failed") as caught:
            await RedactionMiddleware(Broken())(SimpleNamespace(method="tools/call"), next_call)
        assert "raw-secret" not in str(caught.value)
    asyncio.run(check())


def test_kubernetes_effective_values(monkeypatch):
    import opspilot.redaction as module
    registry = SecretRegistry()
    monkeypatch.setattr(module, "registry", registry)
    register_kubernetes_credentials(SimpleNamespace(password="kube-password", api_key={"authorization": "Bearer kube-bare"}))
    policy = Redactor(registry)
    assert policy.redact_text("kube-password kube-bare") == "[REDACTED] [REDACTED]"


def test_protocol_error_is_sanitized():
    async def check():
        async def next_call(ctx):
            raise MCPError(-32602, "token=protocol-secret", {"password": "hidden"})
        with pytest.raises(MCPError) as caught:
            await RedactionMiddleware(Redactor())(SimpleNamespace(method="tools/call"), next_call)
        assert caught.value.message == "token=[REDACTED]"
        assert caught.value.data == {"password": REDACTED}
    asyncio.run(check())


def test_refresh_registration_survives_sdk_hook_replacement():
    from opspilot.redaction import install_kubernetes_registration, redactor
    configuration = SimpleNamespace(password=None, api_key={}, refresh_api_key_hook=None)
    count = 0

    def sdk_refresh(current):
        nonlocal count
        count += 1
        current.api_key["authorization"] = f"Bearer synthetic-refresh-secret-{count}"
        # Kubernetes SDK _set_config installs a new function on every refresh.
        current.refresh_api_key_hook = lambda conf: sdk_refresh(conf)

    configuration.refresh_api_key_hook = sdk_refresh
    install_kubernetes_registration(configuration)
    wrapper = configuration.refresh_api_key_hook
    for number in range(1, 4):
        configuration.refresh_api_key_hook(configuration)
        assert configuration.refresh_api_key_hook is wrapper
        assert redactor.redact_text(f"synthetic-refresh-secret-{number}") == REDACTED


def test_refresh_registration_restored_on_replacing_hook_failure():
    from opspilot.redaction import install_kubernetes_registration, redactor
    configuration = SimpleNamespace(password=None, api_key={}, refresh_api_key_hook=None)

    def failing(current):
        current.api_key["authorization"] = "Bearer synthetic-failed-refresh"
        current.refresh_api_key_hook = lambda conf: None
        raise ValueError("refresh failed")

    configuration.refresh_api_key_hook = failing
    install_kubernetes_registration(configuration)
    wrapper = configuration.refresh_api_key_hook
    with pytest.raises(ValueError):
        wrapper(configuration)
    assert configuration.refresh_api_key_hook is wrapper
    assert redactor.redact_text("synthetic-failed-refresh") == REDACTED
    wrapper(configuration)
    assert configuration.refresh_api_key_hook is wrapper


def test_refresh_registration_stays_installed_when_sdk_clears_hook():
    from opspilot.redaction import install_kubernetes_registration
    configuration = SimpleNamespace(password=None, api_key={}, refresh_api_key_hook=None)
    configuration.refresh_api_key_hook = lambda current: setattr(current, "refresh_api_key_hook", None)
    install_kubernetes_registration(configuration)
    wrapper = configuration.refresh_api_key_hook
    wrapper(configuration)
    wrapper(configuration)
    assert configuration.refresh_api_key_hook is wrapper


@pytest.mark.parametrize("failure", ["format", "write", "flush"])
def test_owned_handler_never_dumps_raw_diagnostics(monkeypatch, capsys, failure):
    import io
    from opspilot.redaction import SafeStderrHandler
    stream = io.StringIO()
    handler = SafeStderrHandler(stream)
    handler.setFormatter(RedactingFormatter())
    def broken(*args, **kwargs):
        raise ValueError("private-handler-exception")
    monkeypatch.setattr(logging, "raiseExceptions", True)
    monkeypatch.setattr(handler if failure == "format" else stream, failure, broken)
    record = logging.LogRecord("test", logging.ERROR, "", 0, "password=%s", ("private-raw-args",), None)
    handler.handle(record)
    captured = capsys.readouterr()
    assert captured.err == captured.out == ""
    assert "Logging error" not in stream.getvalue()
    assert "Traceback" not in stream.getvalue()
    assert "private-" not in stream.getvalue()
