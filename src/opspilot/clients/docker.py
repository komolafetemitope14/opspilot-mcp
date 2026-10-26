"""Small synchronous, read-only Docker client configured through the environment."""

from functools import wraps
import re
from typing import Any

import docker


# Docker names use ASCII alphanumerics followed by alphanumerics, _, . or -.
# Bound identifiers even though the Engine does not impose a useful name limit.
MAX_CONTAINER_IDENTIFIER_LENGTH = 255
_CONTAINER_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", re.ASCII)


def _close_after_operation(method):
    """Release the SDK session even when validation, lookup or projection fails."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            self._client.close()
    return wrapped


def validate_container_identifier(name_or_id: str) -> None:
    """Accept a bare Docker name or ID, never a path or URL component."""
    if (
        not isinstance(name_or_id, str)
        or not 1 <= len(name_or_id) <= MAX_CONTAINER_IDENTIFIER_LENGTH
        or _CONTAINER_IDENTIFIER.fullmatch(name_or_id) is None
    ):
        raise ValueError("name_or_id must be a bare container name or ID (1-255 characters)")


class DockerClient:
    def __init__(self) -> None:
        self._client = docker.from_env()

    @_close_after_operation
    def health_check(self) -> bool:
        """Ping the Docker Engine; connection and API errors propagate."""
        return self._client.ping()

    @staticmethod
    def _summary(container: Any) -> dict[str, Any]:
        attrs = container.attrs
        state = attrs.get("State", {})
        return {
            "id": container.id,
            "name": container.name,
            "image": attrs.get("Config", {}).get("Image") or attrs.get("Image"),
            "status": container.status,
            "health": state.get("Health", {}).get("Status"),
        }

    @_close_after_operation
    def list_containers(self) -> list[dict[str, Any]]:
        """Return curated summaries of running and stopped containers."""
        return [self._summary(container) for container in self._client.containers.list(all=True)]

    @_close_after_operation
    def inspect_container(self, name_or_id: str) -> dict[str, Any]:
        """Return curated operational details for a container by name or ID."""
        validate_container_identifier(name_or_id)
        container = self._client.containers.get(name_or_id)
        state = container.attrs.get("State", {})
        return {
            **self._summary(container),
            "running": state.get("Running"),
            "exit_code": state.get("ExitCode"),
            "ports": {
                port: None if bindings is None else [
                    {"HostIp": binding.get("HostIp"), "HostPort": binding.get("HostPort")}
                    for binding in bindings
                ]
                for port, bindings in container.attrs.get("NetworkSettings", {}).get("Ports", {}).items()
            },
        }

    @_close_after_operation
    def container_logs(self, name_or_id: str, tail: int = 100) -> str:
        """Return the last tail log lines, decoding invalid UTF-8 with replacement."""
        if isinstance(tail, bool) or not isinstance(tail, int) or tail <= 0:
            raise ValueError("tail must be a positive integer")
        validate_container_identifier(name_or_id)
        container = self._client.containers.get(name_or_id)
        return container.logs(tail=tail).decode("utf-8", errors="replace")
