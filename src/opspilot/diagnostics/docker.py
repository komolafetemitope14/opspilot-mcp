"""Deterministic diagnostics for Docker container observations."""

from typing import Any


class DockerDiagnostics:
    """Analyze Docker container observations and produce structured findings."""

    def diagnose_container(
        self,
        container: dict[str, Any],
    ) -> dict[str, Any]:
        """Diagnose one Docker container from its curated inspection data."""
        findings: list[dict[str, Any]] = []

        if not container["running"]:
            findings.append(
                {
                    "code": "container_not_running",
                    "severity": "warning",
                    "summary": "Container is not running.",
                    "evidence": [
                        f"Container status is {container['status']}."
                    ],
                    "recommendation": (
                        "Inspect the container state and logs "
                        "to determine why it is not running."
                    ),
                }
            )

        exit_code = container["exit_code"]

        if exit_code not in (None, 0):
            findings.append(
                {
                    "code": "container_nonzero_exit",
                    "severity": "error",
                    "summary": "Container exited with a non-zero exit code.",
                    "evidence": [
                        f"Container exit code is {exit_code}."
                    ],
                    "recommendation": (
                        "Inspect the container logs, command, "
                        "configuration, and application dependencies."
                    ),
                }
            )

        health = container["health"]

        if health == "unhealthy":
            findings.append(
                {
                    "code": "container_unhealthy",
                    "severity": "error",
                    "summary": "Container health check is failing.",
                    "evidence": [
                        "Docker reports the container health as unhealthy."
                    ],
                    "recommendation": (
                        "Inspect the container health check, logs, "
                        "and application dependencies."
                    ),
                }
            )

        elif health == "starting":
            findings.append(
                {
                    "code": "container_health_starting",
                    "severity": "warning",
                    "summary": "Container health check is still starting.",
                    "evidence": [
                        "Docker reports the container health as starting."
                    ],
                    "recommendation": (
                        "Wait for the health check to complete and inspect "
                        "the container if it does not become healthy."
                    ),
                }
            )

        severities = {
            finding["severity"]
            for finding in findings
        }

        if "error" in severities:
            status = "unhealthy"
        elif "warning" in severities:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "status": status,
            "system": "docker",
            "resource": {
                "kind": "Container",
                "name": container["name"],
            },
            "findings": findings,
        }
