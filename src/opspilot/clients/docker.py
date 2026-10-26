"""Small synchronous, read-only Docker client configured through the environment."""

from functools import wraps
import re
from typing import Any

import docker
from requests.exceptions import Timeout

from opspilot.configuration import DockerSettings, load_docker_settings
from opspilot.limits import (
    DOCKER_REQUEST_TIMEOUT_SECONDS, MAX_LIST_ITEMS, DEFAULT_LOG_TAIL,
    ObservationBudget, ObservationLimitError, ObservationTimeoutError,
    checked_observation, validate_tail, check_log,
)


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
        except Timeout:
            raise ObservationTimeoutError() from None
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
    def __init__(self, configuration: DockerSettings | None = None) -> None:
        settings = configuration if configuration is not None else load_docker_settings()
        # SDK installs this timeout before API version negotiation. It is a
        # native HTTP timeout, not a total deadline; SSH activation and DNS
        # retain SDK/OS behavior.
        self._client = docker.DockerClient(
            **settings.client_kwargs(), timeout=DOCKER_REQUEST_TIMEOUT_SECONDS)

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
        # The low-level list endpoint returns summaries without N+1 inspections.
        # docker-py has already buffered the HTTP response before these checks.
        containers = self._client.api.containers(all=True, limit=MAX_LIST_ITEMS + 1)
        summaries = []
        budget = ObservationBudget().start_list()
        for attrs in containers:
            if len(summaries) >= MAX_LIST_ITEMS:
                raise ObservationLimitError()
            status_text = attrs.get("Status", "")
            health = next((value for value in ("unhealthy", "healthy", "starting")
                           if f"({value})" in status_text
                           or (value == "starting" and "(health: starting)" in status_text)), None)
            summary = {
                "id": attrs.get("Id"),
                "name": (attrs.get("Names") or [None])[0],
                "image": attrs.get("Image"),
                "status": attrs.get("State"),
                "health": health,
            }
            if summary["name"] is not None:
                summary["name"] = summary["name"].removeprefix("/")
            budget.add_item(summary)
            summaries.append(summary)
        return summaries

    @_close_after_operation
    def inspect_container(self, name_or_id: str) -> dict[str, Any]:
        """Return curated operational details for a container by name or ID."""
        validate_container_identifier(name_or_id)
        container = self._client.containers.get(name_or_id)
        state = container.attrs.get("State", {})
        return checked_observation({
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
        })

    @_close_after_operation
    def container_logs(self, name_or_id: str, tail: int = DEFAULT_LOG_TAIL) -> str:
        """Return the last tail log lines, decoding invalid UTF-8 with replacement."""
        validate_tail(tail)
        validate_container_identifier(name_or_id)
        container = self._client.containers.get(name_or_id)
        # Non-following, buffered SDK call. Tail is not a byte bound; this
        # check cannot prevent docker-py's earlier response buffering.
        return check_log(container.logs(tail=tail, follow=False).decode("utf-8", errors="replace"))
