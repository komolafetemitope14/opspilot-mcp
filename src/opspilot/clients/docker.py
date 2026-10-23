"""Small synchronous, read-only Docker client configured through the environment."""

from typing import Any

import docker


class DockerClient:
    def __init__(self) -> None:
        self._client = docker.from_env()

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

    def list_containers(self) -> list[dict[str, Any]]:
        """Return curated summaries of running and stopped containers."""
        return [self._summary(container) for container in self._client.containers.list(all=True)]

    def inspect_container(self, name_or_id: str) -> dict[str, Any]:
        """Return curated operational details for a container by name or ID."""
        container = self._client.containers.get(name_or_id)
        state = container.attrs.get("State", {})
        return {
            **self._summary(container),
            "running": state.get("Running"),
            "exit_code": state.get("ExitCode"),
            "ports": container.attrs.get("NetworkSettings", {}).get("Ports", {}),
        }

    def container_logs(self, name_or_id: str, tail: int = 100) -> str:
        """Return the last tail log lines, decoding invalid UTF-8 with replacement."""
        if isinstance(tail, bool) or not isinstance(tail, int) or tail <= 0:
            raise ValueError("tail must be a positive integer")
        container = self._client.containers.get(name_or_id)
        return container.logs(tail=tail).decode("utf-8", errors="replace")
