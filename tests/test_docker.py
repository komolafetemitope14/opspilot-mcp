import json
from unittest.mock import MagicMock, call

import docker as docker_sdk
import pytest

from opspilot.clients import docker


@pytest.fixture
def engine(monkeypatch):
    from_env = MagicMock()
    monkeypatch.setattr(docker, "load_docker_settings", MagicMock(return_value=docker.DockerSettings("unix:///tmp/test.sock")))
    monkeypatch.setattr(docker.docker, "DockerClient", from_env)
    return from_env, from_env.return_value


@pytest.fixture
def container():
    result = MagicMock()
    result.id = "abc123"
    result.name = "web"
    result.status = "running"
    result.attrs = {
        "Config": {"Image": "nginx:latest", "Env": ["SECRET=private"]},
        "Image": "sha256:image-id",
        "State": {"Running": True, "ExitCode": 0, "Health": {"Status": "healthy"}},
        "NetworkSettings": {
            "Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]},
        },
        "Mounts": [{"Source": "/private"}],
    }
    return result


def test_construction_uses_validated_settings(engine):
    from_env, _ = engine
    docker.DockerClient()
    from_env.assert_called_once_with(base_url="unix:///tmp/test.sock")


def test_construction_errors_propagate(engine):
    from_env, _ = engine
    from_env.side_effect = docker_sdk.errors.DockerException("unavailable")
    with pytest.raises(docker_sdk.errors.DockerException, match="unavailable"):
        docker.DockerClient()


def test_health_check(engine):
    _, sdk = engine
    sdk.ping.return_value = True
    assert docker.DockerClient().health_check() is True
    sdk.ping.assert_called_once_with()


def test_health_check_error_propagates(engine):
    _, sdk = engine
    sdk.ping.side_effect = docker_sdk.errors.APIError("unavailable")
    with pytest.raises(docker_sdk.errors.APIError, match="unavailable"):
        docker.DockerClient().health_check()


def test_list_containers_includes_running_and_stopped(engine, container):
    _, sdk = engine
    stopped = MagicMock()
    stopped.id = "def456"
    stopped.name = "worker"
    stopped.status = "exited"
    stopped.attrs = {"Image": "sha256:worker", "State": {"Running": False}}
    sdk.containers.list.return_value = [container, stopped]
    result = docker.DockerClient().list_containers()
    sdk.containers.list.assert_called_once_with(all=True)
    assert result == [
        {"id": "abc123", "name": "web", "image": "nginx:latest",
         "status": "running", "health": "healthy"},
        {"id": "def456", "name": "worker", "image": "sha256:worker",
         "status": "exited", "health": None},
    ]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("name_or_id", ["web", "abc123"])
@pytest.mark.parametrize("has_health", [True, False])
def test_inspect_container(engine, container, name_or_id, has_health):
    _, sdk = engine
    if not has_health:
        del container.attrs["State"]["Health"]
    sdk.containers.get.return_value = container
    result = docker.DockerClient().inspect_container(name_or_id)
    sdk.containers.get.assert_called_once_with(name_or_id)
    assert result == {
        "id": "abc123", "name": "web", "image": "nginx:latest",
        "status": "running", "health": "healthy" if has_health else None,
        "running": True, "exit_code": 0,
        "ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]},
    }
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("kwargs, expected_tail", [({}, 100), ({"tail": 25}, 25)])
def test_logs_decode_bytes_and_limit_tail(engine, container, kwargs, expected_tail):
    _, sdk = engine
    sdk.containers.get.return_value = container
    container.logs.return_value = "ready café\n".encode("utf-8")
    assert docker.DockerClient().container_logs("web", **kwargs) == "ready café\n"
    sdk.containers.get.assert_called_once_with("web")
    container.logs.assert_called_once_with(tail=expected_tail)


def test_logs_replace_invalid_utf8(engine, container):
    _, sdk = engine
    sdk.containers.get.return_value = container
    container.logs.return_value = b"log\xff"
    assert docker.DockerClient().container_logs("abc123") == "log\ufffd"


@pytest.mark.parametrize("tail", [0, -1, 1.5, "100", None, True, False])
def test_invalid_tail_rejected_before_docker_call(engine, tail):
    _, sdk = engine
    client = docker.DockerClient()
    with pytest.raises(ValueError, match="positive integer"):
        client.container_logs("web", tail=tail)
    assert sdk.mock_calls == [call.close()]


@pytest.mark.parametrize("method", ["inspect_container", "container_logs"])
def test_lookup_errors_propagate(engine, method):
    _, sdk = engine
    sdk.containers.get.side_effect = docker_sdk.errors.NotFound("missing")
    with pytest.raises(docker_sdk.errors.NotFound, match="missing"):
        getattr(docker.DockerClient(), method)("missing")


@pytest.mark.parametrize("method", ["list_containers", "container_logs"])
def test_api_errors_propagate(engine, container, method):
    _, sdk = engine
    sdk.containers.get.return_value = container
    sdk.containers.list.side_effect = docker_sdk.errors.APIError("failed")
    container.logs.side_effect = docker_sdk.errors.APIError("failed")
    with pytest.raises(docker_sdk.errors.APIError, match="failed"):
        if method == "list_containers":
            docker.DockerClient().list_containers()
        else:
            docker.DockerClient().container_logs("web")


@pytest.mark.parametrize("identifier", [
    "web", "a", "web..other", "Web_01.test-service", "0", "abc123", "a" * 64, "a" * 255,
])
@pytest.mark.parametrize("method", ["inspect_container", "container_logs"])
def test_valid_identifiers_reach_lookup_unchanged(engine, container, identifier, method):
    _, sdk = engine
    sdk.containers.get.return_value = container
    container.logs.return_value = b"ready"
    getattr(docker.DockerClient(), method)(identifier)
    sdk.containers.get.assert_called_once_with(identifier)
    sdk.close.assert_called_once_with()


@pytest.mark.parametrize("identifier", [
    "", " ", " web", "web ", "/web", "web/other", r"web\other",
    ".", "..", "../web", "web/../other", "web%2fother",
    "web?tail=all", "web#fragment", "web:latest", "web\n", "web\r",
    "web\x00", "web\t", "web\x7f", "wéb", "-web", "_web", ".web",
    "a" * 256, None, 123, True,
])
@pytest.mark.parametrize("method", ["inspect_container", "container_logs"])
def test_invalid_identifiers_never_reach_sdk_lookup(engine, identifier, method):
    _, sdk = engine
    with pytest.raises(ValueError, match="bare container name or ID"):
        getattr(docker.DockerClient(), method)(identifier)
    assert sdk.mock_calls == [call.close()]


@pytest.mark.parametrize("method", ["health_check", "list_containers", "inspect_container", "container_logs"])
@pytest.mark.parametrize("fails", [False, True])
def test_operation_closes_sdk_and_only_observes(engine, container, method, fails):
    _, sdk = engine
    sdk.containers.list.return_value = [container]
    sdk.containers.get.return_value = container
    container.logs.return_value = b"ready"
    operation = {
        "health_check": sdk.ping,
        "list_containers": sdk.containers.list,
        "inspect_container": sdk.containers.get,
        "container_logs": container.logs,
    }[method]
    if fails:
        operation.side_effect = docker_sdk.errors.APIError("failed")
    args = ("web",) if method in ("inspect_container", "container_logs") else ()
    if fails:
        with pytest.raises(docker_sdk.errors.APIError):
            getattr(docker.DockerClient(), method)(*args)
    else:
        getattr(docker.DockerClient(), method)(*args)
    expected = {
        "health_check": [call.ping(), call.close()],
        "list_containers": [call.containers.list(all=True), call.close()],
        "inspect_container": [call.containers.get("web"), call.close()],
        "container_logs": [call.containers.get("web"), call.containers.get().logs(tail=100), call.close()],
    }[method]
    assert sdk.mock_calls == expected
    assert container.method_calls == ([call.logs(tail=100)] if method == "container_logs" else [])


def test_sensitive_inspection_fields_are_projected_out(engine, container):
    _, sdk = engine
    secret = "sensitive-sentinel"
    container.attrs["Config"].update({
        "Env": [secret], "Labels": {"token": secret}, "Cmd": [secret],
        "Entrypoint": [secret], "Healthcheck": {"Test": [secret]},
    })
    container.attrs.update({"HostConfig": {"Binds": [secret]}, "raw": secret})
    container.attrs["Mounts"] = [{"Source": secret}]
    container.attrs["State"]["Health"]["Log"] = [{"Output": secret}]
    container.attrs["NetworkSettings"]["Ports"]["80/tcp"][0]["extra"] = secret
    container.attrs["NetworkSettings"]["Ports"]["443/tcp"] = None
    sdk.containers.get.return_value = container
    sdk.containers.list.return_value = [container]
    listing = docker.DockerClient().list_containers()
    inspection = docker.DockerClient().inspect_container("web")
    assert secret not in json.dumps([listing, inspection])
    assert set(listing[0]) == {"id", "name", "image", "status", "health"}
    assert set(inspection) == {"id", "name", "image", "status", "health", "running", "exit_code", "ports"}
    assert inspection["ports"] == {
        "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}], "443/tcp": None,
    }
    inspection["ports"]["80/tcp"][0]["HostPort"] = "changed"
    assert container.attrs["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"] == "8080"


def test_projection_exception_still_closes_sdk(engine, container):
    _, sdk = engine
    sdk.containers.get.return_value = container
    container.attrs["NetworkSettings"]["Ports"] = None
    with pytest.raises(AttributeError):
        docker.DockerClient().inspect_container("web")
    sdk.close.assert_called_once_with()


def test_diagnostic_tool_sdk_calls_are_inspection_only(engine, container):
    from mcp.server import MCPServer
    from opspilot.tools.diagnostics import register_diagnostic_tools

    _, sdk = engine
    sdk.containers.get.return_value = container
    server = MCPServer("test")
    register_diagnostic_tools(server)
    result = server._tool_manager._tools["diagnose_docker_issues"].fn("web")
    assert result["status"] == "healthy"
    assert sdk.mock_calls == [call.containers.get("web"), call.close()]
    assert container.method_calls == []


def test_configuration_failure_prevents_client_and_transport_construction(monkeypatch):
    from opspilot.configuration import ConfigurationError
    loader = MagicMock(side_effect=ConfigurationError("DOCKER_HOST is invalid"))
    forbidden = MagicMock(side_effect=AssertionError("Must not activate Docker"))
    monkeypatch.setattr(docker, "load_docker_settings", loader)
    for name in ("DockerClient", "APIClient", "from_env"):
        monkeypatch.setattr(docker_sdk, name, forbidden)
    for name in ("UnixHTTPAdapter", "SSHHTTPAdapter", "NpipeHTTPAdapter"):
        # SSH/named-pipe adapters are optional platform/dependency imports.
        monkeypatch.setattr(docker_sdk.api.client, name, forbidden, raising=False)
    monkeypatch.setattr(docker_sdk.api.client.requests.adapters, "HTTPAdapter", forbidden)
    with pytest.raises(ConfigurationError):
        docker.DockerClient()
    loader.assert_called_once_with()
    forbidden.assert_not_called()
