"""Local validation contracts: synthetic inputs, no infrastructure or plugins."""

import base64
from dataclasses import FrozenInstanceError
import json
import socket
import subprocess
from unittest.mock import MagicMock

import docker
from kubernetes import client
import psycopg
import pytest
import yaml

from opspilot import configuration as cfg
from opspilot.redaction import redactor


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Validation must not activate infrastructure")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(docker, "from_env", forbidden)
    monkeypatch.setattr(docker, "DockerClient", forbidden)
    monkeypatch.setattr(client, "ApiClient", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cfg, "find_dotenv", lambda: "")


@pytest.fixture
def pg():
    return dict(POSTGRES_HOST="localhost", POSTGRES_PORT="5432", POSTGRES_DB="example",
                POSTGRES_USER="reader", POSTGRES_PASSWORD="synthetic-password")


@pytest.mark.parametrize("name", ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"])
@pytest.mark.parametrize("value", [None, "", " \t\n", "bad\x00value"])
def test_postgres_required_and_nul(pg, name, value):
    if value is None:
        pg.pop(name)
    else:
        pg[name] = value
    with pytest.raises(cfg.ConfigurationError, match=name) as caught:
        cfg.load_postgres_config(pg)
    assert "synthetic-password" not in str(caught.value)
    assert "bad\x00value" not in str(caught.value)
    assert redactor.redact_text(pg.get("POSTGRES_PASSWORD", "")) != "synthetic-password"


@pytest.mark.parametrize("port", ["1", "65535", "0005432"])
def test_postgres_valid_ports(pg, port):
    pg["POSTGRES_PORT"] = port
    assert cfg.load_postgres_config(pg).port == port


@pytest.mark.parametrize("port", ["word", "+1", "-1", "1.5", "١٢", "１２", "0", "65536", "999999999999999999", " 5432", "5432 ", "1,2"])
def test_postgres_invalid_ports(pg, port):
    pg["POSTGRES_PORT"] = port
    with pytest.raises(cfg.ConfigurationError, match="POSTGRES_PORT"):
        cfg.load_postgres_config(pg)


@pytest.mark.parametrize("db", ["postgres://reader:hidden@host/db", "postgresql://host/db", "host=localhost dbname=x", "dbname=x"])
def test_postgres_rejects_connection_strings(pg, db):
    pg["POSTGRES_DB"] = db
    with pytest.raises(cfg.ConfigurationError, match="POSTGRES_DB") as caught:
        cfg.load_postgres_config(pg)
    assert db not in str(caught.value)


def test_postgres_exact_password_safe_immutable_and_ambient_boundary(pg):
    pg.update(POSTGRES_PASSWORD="  synthetic secret  ", POSTGRES_ADMIN_PASSWORD="  synthetic secret  ",
              PGSSLMODE="SDK-owned", PGSERVICE="SDK-owned")
    settings = cfg.load_postgres_config(pg)
    assert settings.password == pg["POSTGRES_PASSWORD"]
    assert set(settings.connection_kwargs()) == {"host", "port", "dbname", "user", "password"}
    assert "synthetic" not in repr(settings)
    with pytest.raises(FrozenInstanceError):
        settings.password = "changed"


def test_postgres_admin_never_substitutes(pg):
    pg.update(POSTGRES_ADMIN_USER="admin", POSTGRES_ADMIN_PASSWORD="synthetic-admin")
    pg.pop("POSTGRES_USER")
    pg.pop("POSTGRES_PASSWORD")
    with pytest.raises(cfg.ConfigurationError, match="POSTGRES_USER"):
        cfg.load_postgres_config(pg)
    assert redactor.redact_text("synthetic-admin") == "[REDACTED]"


def test_postgres_equal_admin_user(pg):
    pg["POSTGRES_ADMIN_USER"] = pg["POSTGRES_USER"]
    with pytest.raises(cfg.ConfigurationError, match="POSTGRES_ADMIN_USER"):
        cfg.load_postgres_config(pg)


def test_dotenv_precedence_interpolation_and_no_mutation(pg, tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('BASE=file\nPOSTGRES_HOST=${BASE}\nPOSTGRES_DB=${POSTGRES_HOST}_db\n'
                    'POSTGRES_PASSWORD="  ${BASE} secret  "\nDOCKER_HOST=tcp://unused:1234\n'
                    'KUBECONFIG=unused\nHTTP_PROXY=http://unused:1234\n')
    env = {k: v for k, v in pg.items() if k not in {"POSTGRES_DB", "POSTGRES_PASSWORD"}}
    env["BASE"] = "exported"
    before = dict(cfg.os.environ)
    settings = cfg.load_postgres_config(env, path)
    assert settings.host == "localhost"
    assert settings.dbname == "localhost_db"
    assert settings.password == "  exported secret  "
    assert dict(cfg.os.environ) == before
    env["POSTGRES_PASSWORD"] = ""
    with pytest.raises(cfg.ConfigurationError, match="POSTGRES_PASSWORD"):
        cfg.load_postgres_config(env, path)


def test_dotenv_previous_values_and_default_expansion(pg, tmp_path):
    path = tmp_path / ".env"
    path.write_text('BASE=local\nPOSTGRES_PASSWORD=${BASE}-${ABSENT:-fallback}\n')
    pg.pop("POSTGRES_PASSWORD")
    assert cfg.load_postgres_config(pg, path).password == "local-fallback"
    pg["PYTHON_DOTENV_DISABLED"] = "true"
    with pytest.raises(cfg.ConfigurationError, match="POSTGRES_PASSWORD"):
        cfg.load_postgres_config(pg, path)


def test_malformed_dotenv_is_safe(pg, tmp_path):
    path = tmp_path / ".env"
    path.write_text('POSTGRES_PASSWORD=synthetic-dotenv-secret\nBAD="unclosed\n')
    with pytest.raises(cfg.ConfigurationError) as caught:
        cfg.load_postgres_config(pg, path)
    assert "synthetic-dotenv-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.fixture
def kube():
    return {"apiVersion": "v1", "kind": "Config", "current-context": "reader",
            "contexts": [{"name": "reader", "context": {"cluster": "cluster", "user": "reader"}}],
            "clusters": [{"name": "cluster", "cluster": {"server": "https://cluster.example:6443"}}],
            "users": [{"name": "reader", "user": {"token": "synthetic-kube-token"}}]}


def load_kube(tmp_path, data):
    path = tmp_path / "config"
    path.write_text(yaml.safe_dump(data))
    return cfg.load_kubernetes_settings({}, path)


def test_static_kube_token_system_trust_and_safe_snapshot(tmp_path, kube):
    settings = load_kube(tmp_path, kube)
    assert settings.kubeconfig()["users"][0]["user"]["token"] == "synthetic-kube-token"
    assert "synthetic" not in repr(settings)
    assert redactor.redact_text("synthetic-kube-token") == "[REDACTED]"
    copy = settings.kubeconfig()
    copy["users"].clear()
    assert settings.kubeconfig()["users"]
    with pytest.raises(FrozenInstanceError):
        settings._json = "changed"


@pytest.mark.parametrize("change", ["current", "context", "cluster", "user", "empty"])
def test_kube_reference_failures(tmp_path, kube, change):
    if change == "current":
        kube.pop("current-context")
    elif change == "empty":
        kube["users"][0]["user"] = {}
    else:
        kube[{"context": "contexts", "cluster": "clusters", "user": "users"}[change]] = []
    with pytest.raises(cfg.ConfigurationError, match="kubeconfig"):
        load_kube(tmp_path, kube)


@pytest.mark.parametrize("server", [None, "", "http://cluster", "https://user:synthetic-url-secret@cluster", "https://cluster?x", "https://cluster#x", "https://cluster?", "https:///missing", "https://cluster:99999", "https://cluster:0", "https://cluster with space"])
def test_kube_invalid_server(tmp_path, kube, server):
    cluster = kube["clusters"][0]["cluster"]
    if server is None:
        cluster.pop("server")
    else:
        cluster["server"] = server
    with pytest.raises(cfg.ConfigurationError, match="kubeconfig.cluster.server") as caught:
        load_kube(tmp_path, kube)
    assert "synthetic-url-secret" not in str(caught.value)


@pytest.mark.parametrize("value", [True, "false", 0])
def test_kube_insecure_tls(tmp_path, kube, value):
    kube["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = value
    with pytest.raises(cfg.ConfigurationError, match="insecure-skip-tls-verify"):
        load_kube(tmp_path, kube)


@pytest.mark.parametrize("mechanism", ["exec", "auth-provider"])
def test_kube_rejects_dynamic_auth_without_execution(tmp_path, kube, mechanism):
    kube["users"][0]["user"][mechanism] = {"command": "must-not-execute", "name": "oidc"}
    with pytest.raises(cfg.ConfigurationError, match=mechanism):
        load_kube(tmp_path, kube)


@pytest.mark.parametrize("section,key", [("cluster", "certificate-authority"), ("user", "client-certificate"), ("user", "client-key"), ("user", "tokenFile")])
def test_kube_missing_material(tmp_path, kube, section, key):
    target = kube["clusters"][0]["cluster"] if section == "cluster" else kube["users"][0]["user"]
    if key == "tokenFile":
        target.pop("token")
    target[key] = "missing"
    with pytest.raises(cfg.ConfigurationError, match=key):
        load_kube(tmp_path, kube)


@pytest.mark.parametrize("key", ["client-certificate-data", "client-key-data"])
def test_kube_incomplete_pair(tmp_path, kube, key):
    kube["users"][0]["user"][key] = base64.b64encode(b"synthetic-material").decode()
    with pytest.raises(cfg.ConfigurationError, match="supplied together"):
        load_kube(tmp_path, kube)


def test_kube_file_material_is_snapshotted(tmp_path, kube):
    (tmp_path / "token").write_text("synthetic-file-token")
    (tmp_path / "ca").write_text("synthetic-ca")
    kube["users"][0]["user"] = {"tokenFile": "token"}
    kube["clusters"][0]["cluster"]["certificate-authority"] = "ca"
    settings = load_kube(tmp_path, kube)
    assert settings.kubeconfig()["users"][0]["user"] == {"token": "synthetic-file-token"}
    assert redactor.redact_text("synthetic-file-token") == "[REDACTED]"
    assert "certificate-authority" not in settings.kubeconfig()["clusters"][0]["cluster"]


def test_kube_missing_unreadable_and_malformed(tmp_path, monkeypatch):
    with pytest.raises(cfg.ConfigurationError, match="kubeconfig"):
        cfg.load_kubernetes_settings({}, tmp_path / "missing")
    path = tmp_path / "config"
    path.write_text('users: [\ntoken: synthetic-malformed-secret\n')
    with pytest.raises(cfg.ConfigurationError) as caught:
        cfg.load_kubernetes_settings({}, path)
    assert "synthetic-malformed-secret" not in str(caught.value)
    assert caught.value.__suppress_context__
    monkeypatch.setattr(cfg.Path, "read_text", MagicMock(side_effect=PermissionError("synthetic-secret")))
    with pytest.raises(cfg.ConfigurationError, match="readable") as caught:
        cfg.load_kubernetes_settings({}, path)
    assert "synthetic-secret" not in str(caught.value)


def test_kube_path_list_and_call_time_environment(tmp_path, kube):
    first = tmp_path / "first"
    first.write_text(yaml.safe_dump(kube))
    second = tmp_path / "second"
    kube["users"][0]["user"]["token"] = "ignored-duplicate"
    second.write_text(yaml.safe_dump(kube))
    settings = cfg.load_kubernetes_settings({"KUBECONFIG": cfg.os.pathsep.join(map(str, [tmp_path / "missing", first, second]))})
    assert settings.kubeconfig()["users"][0]["user"]["token"] == "synthetic-kube-token"


@pytest.mark.parametrize("host", ["unix:///tmp/synthetic.sock", "tcp://localhost:2375", "ssh://reader@host:22", "https://host:2376"])
def test_docker_explicit_host_is_local(host):
    assert cfg.load_docker_settings({"DOCKER_HOST": host}).endpoint == host


@pytest.mark.parametrize("host", ["   ", "\t", "tcp://", "bad://synthetic-secret", "tcp://host:99999", "tcp://host:notaport"])
def test_docker_invalid_endpoint(host):
    with pytest.raises(cfg.ConfigurationError, match="DOCKER_HOST") as caught:
        cfg.load_docker_settings({"DOCKER_HOST": host})
    assert "synthetic-secret" not in str(caught.value)


def test_docker_default_and_missing_explicit_context():
    assert cfg.load_docker_settings({}).endpoint == "unix:///var/run/docker.sock"
    with pytest.raises(cfg.ConfigurationError, match="DOCKER_CONTEXT"):
        cfg.load_docker_settings({"DOCKER_CONTEXT": "missing"})
    assert cfg.load_docker_settings({"DOCKER_CONTEXT": "missing", "DOCKER_HOST": "unix:///tmp/test.sock"}).endpoint == "unix:///tmp/test.sock"


def test_docker_context_selection(tmp_path, monkeypatch):
    context = MagicMock()
    context.Host = "unix:///tmp/context.sock"
    context.TLSConfig = None
    get = MagicMock(return_value=context)
    monkeypatch.setattr(cfg.ContextAPI, "get_context", get)
    assert cfg.load_docker_settings({"DOCKER_CONTEXT": "synthetic"}).endpoint == context.Host
    assert get.call_args.args == ("synthetic",)


@pytest.mark.parametrize("flag,verify", [(None, False), ("", False), ("0", True), ("false", True), ("1", True)])
def test_docker_tls_semantics(tmp_path, flag, verify):
    for name in ("cert.pem", "key.pem", "ca.pem"):
        (tmp_path / name).write_text("synthetic-pem")
    env = {"DOCKER_HOST": "tcp://host:2376", "DOCKER_CERT_PATH": str(tmp_path)}
    if flag is not None:
        env["DOCKER_TLS_VERIFY"] = flag
    settings = cfg.load_docker_settings(env)
    assert settings.verify is verify
    assert settings.tls_enabled
    assert "synthetic" not in repr(settings)


def test_docker_missing_and_unreadable_tls_files(tmp_path, monkeypatch):
    env = {"DOCKER_HOST": "tcp://host:2376", "DOCKER_CERT_PATH": str(tmp_path)}
    with pytest.raises(cfg.ConfigurationError, match="TLS"):
        cfg.load_docker_settings(env)
    for name in ("cert.pem", "key.pem", "ca.pem"):
        (tmp_path / name).write_text("synthetic-pem")
    monkeypatch.setattr(cfg.Path, "read_text", MagicMock(side_effect=PermissionError("secret")))
    with pytest.raises(cfg.ConfigurationError, match="TLS"):
        cfg.load_docker_settings(env)


def test_postgres_long_zero_port_is_a_safe_configuration_error(pg):
    pg["POSTGRES_PORT"] = "0" * 5000
    with pytest.raises(cfg.ConfigurationError, match="POSTGRES_PORT"):
        cfg.load_postgres_config(pg)


@pytest.mark.parametrize("material", ["certificate-authority-data", "client-key-data"])
def test_kube_invalid_embedded_material(tmp_path, kube, material):
    section = kube["clusters"][0]["cluster"] if material.startswith("certificate-authority") else kube["users"][0]["user"]
    section[material] = "synthetic-invalid-base64!"
    with pytest.raises(cfg.ConfigurationError, match="kubeconfig") as caught:
        load_kube(tmp_path, kube)
    assert "synthetic-invalid-base64" not in str(caught.value)


def test_kube_certificate_pair_and_basic_auth(tmp_path, kube):
    kube["users"][0]["user"] = {
        "client-certificate-data": base64.b64encode(b"synthetic-cert").decode(),
        "client-key-data": base64.b64encode(b"synthetic-private-key").decode(),
    }
    settings = load_kube(tmp_path, kube)
    assert "client-key-data" in settings.kubeconfig()["users"][0]["user"]
    assert redactor.redact_text("synthetic-private-key") == "[REDACTED]"
    kube["users"][0]["user"] = {"username": "reader", "password": "synthetic-basic-secret"}
    load_kube(tmp_path, kube)
    encoded = base64.b64encode(b"reader:synthetic-basic-secret").decode()
    assert redactor.redact_text(encoded) == "[REDACTED]"


def test_kube_file_secrets_registered_before_bad_server(tmp_path, kube):
    (tmp_path / "token").write_text("synthetic-early-file-token")
    kube["users"][0]["user"] = {"tokenFile": "token"}
    kube["clusters"][0]["cluster"]["server"] = "http://invalid"
    with pytest.raises(cfg.ConfigurationError):
        load_kube(tmp_path, kube)
    assert redactor.redact_text("synthetic-early-file-token") == "[REDACTED]"


def test_kube_unreadable_material(tmp_path, kube, monkeypatch):
    key = tmp_path / "key"
    key.write_text("synthetic-key")
    kube["users"][0]["user"]["client-key"] = "key"
    original = cfg.Path.read_text

    def read(path, *args, **kwargs):
        if path == key:
            raise PermissionError("synthetic-error-secret")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(cfg.Path, "read_text", read)
    with pytest.raises(cfg.ConfigurationError, match="client-key") as caught:
        load_kube(tmp_path, kube)
    assert "synthetic-error-secret" not in str(caught.value)


def test_docker_current_context_from_local_metadata(tmp_path, monkeypatch):
    import hashlib
    directory = tmp_path / ".docker"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"currentContext": "synthetic-context"}))
    meta = directory / "contexts" / "meta" / hashlib.sha256(b"synthetic-context").hexdigest()
    meta.mkdir(parents=True)
    (meta / "meta.json").write_text(json.dumps({
        "Name": "synthetic-context", "Metadata": {},
        "Endpoints": {"docker": {"Host": "unix:///tmp/current-context.sock", "SkipTLSVerify": True}},
    }))
    assert cfg.load_docker_settings({}).endpoint == "unix:///tmp/current-context.sock"
    assert cfg.load_docker_settings({"DOCKER_CONTEXT": "default"}).endpoint == "unix:///var/run/docker.sock"


def test_docker_context_endpoint_is_validated(monkeypatch):
    context = MagicMock()
    context.Host = "bad://synthetic-context-secret"
    context.TLSConfig = None
    monkeypatch.setattr(cfg.ContextAPI, "get_context", lambda name=None: context)
    with pytest.raises(cfg.ConfigurationError, match="endpoint"):
        cfg.load_docker_settings({"DOCKER_CONTEXT": "synthetic-context"})


def test_docker_tls_default_directory(tmp_path):
    directory = tmp_path / ".docker"
    directory.mkdir()
    for name in ("cert.pem", "key.pem", "ca.pem"):
        (directory / name).write_text("synthetic-tls-content")
    settings = cfg.load_docker_settings({"DOCKER_HOST": "tcp://host:2376", "DOCKER_TLS_VERIFY": "0"})
    assert settings.verify is True
    assert settings.cert == (str(directory / "cert.pem"), str(directory / "key.pem"))
    assert redactor.redact_text("synthetic-tls-content") == "[REDACTED]"


def test_multiline_dotenv_password_registered_before_syntax_error(pg, tmp_path):
    path = tmp_path / ".env"
    # A valid multiline password followed by invalid syntax has a determinable
    # secret even though the source as a whole cannot be accepted.
    path.write_text('POSTGRES_PASSWORD="synthetic\nmultiline-secret"\nBROKEN="unclosed\n')
    with pytest.raises(cfg.ConfigurationError):
        cfg.load_postgres_config(pg, path)
    assert redactor.redact_text("synthetic\nmultiline-secret") == "[REDACTED]"


@pytest.mark.parametrize("malformed", [False, True])
def test_dotenv_registry_retains_credentials_only(pg, tmp_path, monkeypatch, malformed):
    from opspilot.redaction import SecretRegistry
    isolated = SecretRegistry()
    monkeypatch.setattr(cfg, "registry", isolated)
    source = ('BASE=registry-parsed\nPOSTGRES_PASSWORD="${BASE}\nmultiline-secret"\n'
              'POSTGRES_ADMIN_PASSWORD=registry-recognizable-secret\n')
    if malformed:
        source += 'BROKEN="unclosed\n'
    path = tmp_path / ".env"
    path.write_text(source)
    pg.pop("POSTGRES_PASSWORD")
    if malformed:
        with pytest.raises(cfg.ConfigurationError):
            cfg.load_postgres_config(pg, path)
    else:
        cfg.load_postgres_config(pg, path)
    assert source not in isolated.values()
    assert "registry-recognizable-secret" in isolated.values()
    assert "registry-parsed\nmultiline-secret" in isolated.values()


@pytest.mark.parametrize("malformed", [False, True])
def test_kube_registry_retains_credentials_only(kube, tmp_path, monkeypatch, malformed):
    from opspilot.redaction import SecretRegistry
    isolated = SecretRegistry()
    monkeypatch.setattr(cfg, "registry", isolated)
    source = yaml.safe_dump(kube)
    if malformed:
        source += 'broken: [\n'
    path = tmp_path / "config"
    path.write_text(source)
    if malformed:
        with pytest.raises(cfg.ConfigurationError):
            cfg.load_kubernetes_settings({}, path)
    else:
        cfg.load_kubernetes_settings({}, path)
    assert source not in isolated.values()
    assert "synthetic-kube-token" in isolated.values()
