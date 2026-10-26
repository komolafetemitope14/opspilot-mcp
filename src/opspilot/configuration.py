"""Lazy, local backend configuration. No transports or credentials are activated here.

Only the supported application fields are validated. Ambient libpq service/TLS,
Requests proxy, SSH and cryptographic behavior remain SDK responsibilities.
"""

import base64
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import unquote, urlsplit

import docker
from docker.constants import IS_WINDOWS_PLATFORM
from docker.context import ContextAPI
from dotenv import find_dotenv
from dotenv.parser import parse_stream
from dotenv.variables import parse_variables
import yaml

from opspilot.redaction import registry


class ConfigurationError(ValueError):
    """Application errors use constant field names/reasons, never input values."""


def _required(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} is required and must not be blank")
    if "\x00" in value:
        raise ConfigurationError(f"{name} must not contain NUL characters")
    return value


def _read(path, name):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeError):
        raise ConfigurationError(f"{name} must be a readable file") from None


def _register_source(text):
    # Extract recognizable credentials even from malformed sources.
    # Parser excerpts are never published.
    for match in re.finditer(
        r"(?im)^\s*(?:token|password|client-key-data|POSTGRES_PASSWORD|POSTGRES_ADMIN_PASSWORD)\s*[:=]\s*([^\r\n]+)",
        text,
    ):
        registry.register(match[1].strip().strip("\"'"))


def _register_environment(environ):
    registry.register_environment(environ)
    # These may influence the SDK even though OpsPilot does not parse them.
    registry.register(environ.get("PGPASSWORD"))
    for name in ("DOCKER_HOST", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        value = environ.get(name)
        if value:
            try:
                url = urlsplit(value)
                if url.password:
                    registry.register(url.password)
                    registry.register(unquote(url.password))
            except ValueError:
                pass


@dataclass(frozen=True, repr=False)
class PostgresConfig:
    host: str
    port: str
    dbname: str
    user: str
    password: str = field(repr=False)

    def connection_kwargs(self):
        return dict(host=self.host, port=self.port, dbname=self.dbname,
                    user=self.user, password=self.password)


def load_postgres_config(environ: Mapping[str, str] | None = None,
                         dotenv_path=None) -> PostgresConfig:
    env = dict(os.environ if environ is None else environ)
    _register_environment(env)
    values = {}
    invalid_syntax = False
    disabled = env.get("PYTHON_DOTENV_DISABLED", "").casefold() in {
        "1", "true", "t", "yes", "y",
    }
    if not disabled:
        path = find_dotenv() if dotenv_path is None else dotenv_path
        if path and Path(path).exists():
            text = _read(path, "PostgreSQL dotenv")
            _register_source(text)
            # python-dotenv's parser and expansion grammar, with override=False
            # precedence against our snapshot, without mutating os.environ.
            for binding in parse_stream(io.StringIO(text)):
                if binding.error:
                    invalid_syntax = True
                if binding.key is not None:
                    value = binding.value
                    if value is not None:
                        scope = {**values, **env}
                        value = "".join(atom.resolve(scope) for atom in parse_variables(value))
                    values[binding.key] = value
                    if binding.key in {"POSTGRES_PASSWORD", "POSTGRES_ADMIN_PASSWORD"}:
                        registry.register(value)
    effective = {**values, **env}
    _register_environment(effective)
    if invalid_syntax:
        raise ConfigurationError("PostgreSQL dotenv has invalid syntax") from None
    names = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    for name in names:
        _required(effective.get(name), name)
    port = effective["POSTGRES_PORT"]
    digits = port.lstrip("0") or "0"
    if (not re.fullmatch(r"[0-9]+", port) or len(digits) > 5
            or not 1 <= int(digits) <= 65535):
        raise ConfigurationError("POSTGRES_PORT must be an integer between 1 and 65535")
    db = effective["POSTGRES_DB"]
    if "=" in db or re.match(r"(?i)^\s*postgres(?:ql)?://", db):
        raise ConfigurationError("POSTGRES_DB must be a database name, not a URI or conninfo")
    if effective.get("POSTGRES_ADMIN_USER") == effective["POSTGRES_USER"]:
        raise ConfigurationError("POSTGRES_USER must differ from POSTGRES_ADMIN_USER")
    # Do not copy ambient PG* settings into kwargs or attempt to validate them.
    return PostgresConfig(*(effective[name] for name in names))


@dataclass(frozen=True, repr=False)
class DockerSettings:
    endpoint: str
    # Immutable primitives instead of a mutable SDK TLSConfig.
    cert: tuple[str, str] | None = field(default=None, repr=False)
    ca_cert: str | None = None
    verify: bool | str | None = None
    tls_enabled: bool = False

    def client_kwargs(self):
        kwargs = {"base_url": self.endpoint}
        if self.tls_enabled:
            kwargs["tls"] = docker.tls.TLSConfig(
                client_cert=self.cert, ca_cert=self.ca_cert, verify=self.verify)
        return kwargs


def load_docker_settings(environ: Mapping[str, str] | None = None) -> DockerSettings:
    env = dict(os.environ if environ is None else environ)
    _register_environment(env)
    host = env.get("DOCKER_HOST")
    if host is not None and host and not host.strip():
        raise ConfigurationError("DOCKER_HOST must not be whitespace-only")
    try:
        # Both helpers only read local settings; neither creates a transport or
        # invokes registry credential helpers. Keep docker-py's TLS booleans.
        params = docker.utils.kwargs_from_env(environment=env or {"_OPSPILOT_EMPTY_ENV": "1"})
    except Exception:
        raise ConfigurationError("DOCKER_CERT_PATH/DOCKER_TLS_VERIFY TLS configuration is invalid") from None
    if "base_url" not in params:
        try:
            selected = env.get("DOCKER_CONTEXT")
            if selected:
                resolved = ContextAPI.get_context(selected)
                if resolved is None:
                    raise ConfigurationError("DOCKER_CONTEXT does not select an existing context")
                context = {"base_url": resolved.Host} if resolved.Host else {}
                if resolved.TLSConfig is not None:
                    context["tls"] = resolved.TLSConfig
            else:
                context = ContextAPI.kwargs_from_context(environment=env)
            for key, value in context.items():
                params.setdefault(key, value)
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("Docker context configuration is invalid") from None
    endpoint = params.get("base_url")
    tls = params.get("tls")
    _register_environment({"DOCKER_HOST": endpoint})
    try:
        docker.utils.parse_host(endpoint, IS_WINDOWS_PLATFORM, tls=bool(tls))
    except Exception:
        raise ConfigurationError("DOCKER_HOST or Docker context endpoint is invalid") from None
    if tls:
        for path in (*(tls.cert or ()), *((tls.ca_cert,) if tls.verify and tls.ca_cert else ())):
            content = _read(path, "Docker TLS certificate/key files")
            if tls.cert and path == tls.cert[1]:
                registry.register(content)
    # Preserve the original endpoint for docker-py's transport normalization.
    if endpoint is None:
        endpoint = docker.constants.DEFAULT_NPIPE if IS_WINDOWS_PLATFORM else "unix:///var/run/docker.sock"
    return DockerSettings(endpoint, tls.cert if tls else None, tls.ca_cert if tls else None,
                          tls.verify if tls else None, bool(tls))


@dataclass(frozen=True, repr=False)
class KubernetesSettings:
    # A selected standalone snapshot avoids re-reading a changed source between
    # validation and activation. JSON is immutable and never included in repr.
    _json: str = field(repr=False)

    def kubeconfig(self):
        return json.loads(self._json)


def _register_kube(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"token", "password", "client-key-data", "id-token", "refresh-token", "client-secret"}:
                registry.register(child)
                if key == "client-key-data" and isinstance(child, str):
                    try:
                        registry.register(base64.b64decode(child, validate=True).decode("utf-8"))
                    except (ValueError, UnicodeError):
                        pass
            _register_kube(child)
    elif isinstance(value, list):
        for child in value:
            _register_kube(child)


def _material(section, file_key, data_key, base, name, *, secret=False):
    if data_key in section:
        value = _required(section[data_key], name)
        if secret:
            registry.register(value)
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise ConfigurationError(f"{name} must contain valid encoded material") from None
        if secret:
            registry.register(decoded)
        _required(decoded, name)
        return True
    if file_key in section:
        path = _required(section[file_key], name)
        content = _read(base / path, name)
        if secret:
            registry.register(content)
        _required(content, name)
        # Snapshot into the selected configuration: no second file read by SDK.
        section.pop(file_key)
        section[data_key] = base64.b64encode(content.encode()).decode()
        return True
    return False


def load_kubernetes_settings(environ: Mapping[str, str] | None = None,
                             config_file=None) -> KubernetesSettings:
    env = dict(os.environ if environ is None else environ)
    _register_environment(env)
    paths = config_file if config_file is not None else env.get("KUBECONFIG", "~/.kube/config")
    # Supported static projection of SDK 34 merge rules: first named entry wins,
    # last current-context wins, and file references are relative to their source.
    entries = {"clusters": {}, "users": {}, "contexts": {}}
    current = None
    found = False
    try:
        for raw_path in os.fspath(paths).split(os.pathsep):
            if not raw_path:
                continue
            path = Path(raw_path).expanduser()
            if not path.exists():
                continue  # SDK path-list semantics; require at least one below.
            found = True
            text = _read(path, "kubeconfig")
            _register_source(text)
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                raise ConfigurationError("kubeconfig has invalid syntax") from None
            _register_kube(data)
            if not isinstance(data, dict):
                raise ConfigurationError("kubeconfig must contain a configuration mapping")
            for kind in entries:
                items = data.get(kind) or []
                if not isinstance(items, list):
                    raise ConfigurationError("kubeconfig references are invalid")
                for item in items:
                    name = item["name"]
                    if not isinstance(name, str) or not name.strip():
                        raise ConfigurationError("kubeconfig references are invalid")
                    entries[kind].setdefault(name, (item, path.absolute().parent))
            if "current-context" in data:
                current = data["current-context"]
        if not found:
            raise ConfigurationError("kubeconfig must select a readable configuration file")
        _required(current, "kubeconfig.current-context")
        context, _ = entries["contexts"][current]
        context = context["context"]
        cluster_entry, cluster_base = entries["clusters"][context["cluster"]]
        user_entry, user_base = entries["users"][context["user"]]
        cluster = cluster_entry["cluster"]
        user = user_entry["user"]
        if not isinstance(cluster, dict) or not isinstance(user, dict):
            raise ConfigurationError("kubeconfig cluster/user references are invalid")
    except ConfigurationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ConfigurationError("kubeconfig current-context/cluster/user references are invalid") from None
    # Register available selected file credentials before any subsequent field
    # validation. Failed reads are reported by the field-specific checks below.
    for file_key in ("tokenFile", "client-key"):
        value = user.get(file_key)
        if isinstance(value, str) and value:
            try:
                registry.register((user_base / value).read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                pass
    if isinstance(user.get("username"), str) and isinstance(user.get("password"), str):
        registry.register(base64.b64encode(f"{user['username']}:{user['password']}".encode()).decode())
    for mechanism in ("exec", "auth-provider"):
        if mechanism in user:
            raise ConfigurationError(f"kubeconfig.user.{mechanism} is not supported; use static credentials")
    server = _required(cluster.get("server"), "kubeconfig.cluster.server")
    try:
        url = urlsplit(server)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or "?" in server or "#" in server
                or any(c.isspace() or ord(c) < 32 for c in server)
                or (url.port is not None and not 1 <= url.port <= 65535)):
            raise ValueError()
    except ValueError:
        raise ConfigurationError("kubeconfig.cluster.server must be an absolute HTTPS URL without userinfo/query/fragment") from None
    if cluster.get("insecure-skip-tls-verify", False) is not False:
        raise ConfigurationError("kubeconfig.cluster.insecure-skip-tls-verify must be false")
    if "tls-server-name" in cluster:
        _required(cluster["tls-server-name"], "kubeconfig.cluster.tls-server-name")
    _material(cluster, "certificate-authority", "certificate-authority-data", cluster_base,
              "kubeconfig.cluster.certificate-authority")
    cert = _material(user, "client-certificate", "client-certificate-data", user_base,
                     "kubeconfig.user.client-certificate")
    key = _material(user, "client-key", "client-key-data", user_base,
                    "kubeconfig.user.client-key", secret=True)
    if cert != key:
        raise ConfigurationError("kubeconfig.user.client-certificate/client-key must be supplied together")
    token = user.get("token")
    if not token and "tokenFile" in user:
        token = _read(user_base / _required(user["tokenFile"], "kubeconfig.user.tokenFile"),
                      "kubeconfig.user.tokenFile")
        registry.register(token)
        user["token"] = token
    has_token = "token" in user
    if has_token:
        _required(token, "kubeconfig.user.token")
    basic = "username" in user or "password" in user
    if basic:
        _required(user.get("username"), "kubeconfig.user.username")
        _required(user.get("password"), "kubeconfig.user.password")
        registry.register(base64.b64encode(f"{user['username']}:{user['password']}".encode()).decode())
    if not (has_token or basic or (cert and key)):
        raise ConfigurationError("kubeconfig.user authentication requires static token, password or certificate/key")
    # Drop extensions and unused mechanisms; activation only sees supported data.
    cluster = {k: v for k, v in cluster.items() if k in {
        "server", "certificate-authority-data", "tls-server-name", "insecure-skip-tls-verify"}}
    user = {k: v for k, v in user.items() if k in {
        "token", "username", "password", "client-certificate-data", "client-key-data"}}
    snapshot = {"apiVersion": "v1", "kind": "Config", "current-context": "selected",
                "contexts": [{"name": "selected", "context": {"cluster": "selected", "user": "selected"}}],
                "clusters": [{"name": "selected", "cluster": cluster}],
                "users": [{"name": "selected", "user": user}]}
    try:
        return KubernetesSettings(json.dumps(snapshot))
    except (TypeError, ValueError):
        raise ConfigurationError("kubeconfig static configuration is invalid") from None
