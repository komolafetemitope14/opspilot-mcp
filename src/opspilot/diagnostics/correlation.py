"""Deterministic correlation of normalized infrastructure diagnoses."""

from typing import Any


class CorrelationDiagnostics:
    """Correlate diagnostic results from multiple infrastructure systems."""

    def correlate(
        self,
        kubernetes: dict[str, Any],
        postgres: dict[str, Any],
    ) -> dict[str, Any]:
        """Correlate Kubernetes and PostgreSQL diagnostic results."""
        findings: list[dict[str, Any]] = []

        if (
            kubernetes["status"] == "unhealthy"
            and postgres["status"] == "unhealthy"
        ):
            findings.append(
                {
                    "code": "possible_database_dependency_failure",
                    "severity": "error",
                    "summary": (
                        "An application runtime failure and PostgreSQL "
                        "failure were detected together."
                    ),
                    "evidence": [
                        "Kubernetes diagnosis is unhealthy.",
                        "PostgreSQL diagnosis is unhealthy.",
                    ],
                    "recommendation": (
                        "Check whether the application depends on PostgreSQL "
                        "and inspect its database connection configuration "
                        "and logs."
                    ),
                }
            )

        status = "unhealthy" if findings else "healthy"

        return {
            "status": status,
            "system": "cross-system",
            "resource": {
                "kind": "Correlation",
            },
            "findings": findings,
        }
