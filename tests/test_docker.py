import json
from unittest.mock import MagicMock

import docker as docker_sdk
import pytest

from opspilot.clients import docker


@pytest.fixture
def engine(monkeypatch):
    from_env = MagicMock()
    monkeypatch.setattr(docker.docker, "from_env", from_env)
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


def test_construction_uses_from_env(engine):
    from_env, _ = engine
    docker.DockerClient()
    from_env.assert_called_once_with()


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
    assert sdk.mock_calls == []


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
