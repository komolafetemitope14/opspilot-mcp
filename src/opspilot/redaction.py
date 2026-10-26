"""Central output policy. Raw client observations remain trusted internal data."""

import logging
import re
from threading import RLock
from urllib.parse import unquote

from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

REDACTED = "[REDACTED]"
_KEYS = {
    "password", "passwd", "token", "apikey", "clientsecret", "authorization",
    "accesstoken", "refreshtoken", "privatekey",
}
# Explicit configuration names, not a scan of arbitrary environment values.
SECRET_ENV_NAMES = (
    "POSTGRES_PASSWORD", "POSTGRES_ADMIN_PASSWORD", "API_KEY", "API_TOKEN",
    "ACCESS_TOKEN", "REFRESH_TOKEN", "CLIENT_SECRET",
)


def credential_key(key: str) -> bool:
    return re.sub(r"[_\-\s]", "", key).casefold() in _KEYS


class SecretRegistry:
    def __init__(self):
        self._values = set()
        self._lock = RLock()

    def register(self, value):
        if isinstance(value, str) and value and value != REDACTED:
            with self._lock:
                self._values.add(value)

    def register_environment(self, environ):
        for name in SECRET_ENV_NAMES:
            self.register(environ.get(name))

    def values(self):
        with self._lock:
            return sorted(self._values, key=len, reverse=True)


_KEY_PATTERN = r"(?:password|passwd|token|api[_ -]?key|client[_ -]?secret|authorization|access[_ -]?token|refresh[_ -]?token|private[_ -]?key)"
_ASSIGNMENT = re.compile(
    r"""(?ix)(?<![\w-])(?P<key>""" + _KEY_PATTERN + r""")
    (?P<sep>["']?\s*[:=]\s*)
    (?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[REDACTED\]|[^\s,;&}\]<>]+)"""
)
_AUTH = re.compile(r"(?im)(\bAuthorization\s*:\s*)[^\r\n]+")
_BEARER = re.compile(r"(?i)(\bBearer\s+)(?:\[REDACTED\]|[^\s,;\"'<>}\]]+)")
_PEM = re.compile(r"-----BEGIN (?P<kind>(?:[A-Z0-9]+ )*PRIVATE KEY)-----.*?-----END (?P=kind)-----", re.S)
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+")


class Redactor:
    def __init__(self, registry=None):
        self.registry = registry if registry is not None else SecretRegistry()

    def _url(self, match):
        url = match.group()
        scheme, rest = url.split("://", 1)
        authority, sep, suffix = re.split(r"([/?#])", rest, maxsplit=1) if re.search(r"[/?#]", rest) else (rest, "", "")
        if "@" in authority:
            authority = REDACTED + "@" + authority.rsplit("@", 1)[1]
        url = scheme + "://" + authority + sep + suffix
        return re.sub(
            r"([?&])([^=&?#]+)=([^&#]*)",
            lambda m: m[1] + m[2] + "=" + (REDACTED if credential_key(unquote(m[2])) else m[3]),
            url,
        )

    def redact_text(self, text):
        text = _PEM.sub(REDACTED, text)
        text = _URL.sub(self._url, text)
        text = _AUTH.sub(lambda m: m[1] + REDACTED, text)
        text = _BEARER.sub(lambda m: m[1] + REDACTED, text)

        def assignment(m):
            value = m["value"]
            quote = value[0] if value.startswith(('"', "'")) else ""
            return m["key"] + m["sep"] + quote + REDACTED + quote

        text = _ASSIGNMENT.sub(assignment, text)
        # Protect the marker itself, including for very short registered secrets.
        parts = text.split(REDACTED)
        values = self.registry.values()
        for index, part in enumerate(parts):
            if values:
                part = re.sub("|".join(re.escape(v) for v in values), lambda _: REDACTED, part)
            parts[index] = part
        return REDACTED.join(parts)

    def redact_value(self, value):
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {
                self.redact_text(k) if isinstance(k, str) else k:
                REDACTED if isinstance(k, str) and credential_key(k) else self.redact_value(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return type(value)(self.redact_value(v) for v in value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise TypeError("Unsupported output value")


registry = SecretRegistry()
redactor = Redactor(registry)


class RedactionMiddleware:
    """Public SDK middleware: sanitize converted tool results and protocol errors."""

    def __init__(self, policy=redactor):
        self.policy = policy

    async def __call__(self, ctx, call_next):
        try:
            result = await call_next(ctx)
        except MCPError as exc:
            try:
                message = self.policy.redact_text(exc.message)
                data = self.policy.redact_value(exc.data)
            except Exception:
                raise MCPError(-32603, "Output sanitization failed") from None
            raise MCPError(exc.code, message, data) from None
        except ValidationError:
            # Framework validation messages can contain rejected input values.
            raise MCPError(-32602, "Invalid request parameters") from None
        if ctx.method != "tools/call":
            return result
        try:
            return self.policy.redact_value(result)
        except Exception:
            raise MCPError(-32603, "Output sanitization failed") from None


class RedactingFormatter(logging.Formatter):
    """Sanitize after traceback formatting; never return the raw fallback."""

    def __init__(self, policy=redactor):
        super().__init__("%(levelname)s %(name)s: %(message)s")
        self.policy = policy

    def format(self, record):
        try:
            return self.policy.redact_text(super().format(record))
        except Exception:
            return "Output sanitization failed"


def configure_stderr():
    """Own the server's default stderr handler, without modifying SDK code."""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def register_kubernetes_credentials(configuration):
    """Read effective in-memory credentials; never serialize kubeconfig."""
    registry.register(configuration.password)
    for value in configuration.api_key.values():
        registry.register(value)
        if isinstance(value, str) and value.lower().startswith("bearer "):
            registry.register(value[7:])
