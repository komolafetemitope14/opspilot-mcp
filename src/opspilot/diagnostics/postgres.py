"""Deterministic diagnostics for PostgreSQL observations."""

from typing import Any


class PostgresDiagnostics:
    """Analyze PostgreSQL observations and produce structured findings."""

    def diagnose_database(
        self,
        healthy: bool,
        tables: list[str],
    ) -> dict[str, Any]:
        """Diagnose PostgreSQL health and public-table observations."""
        findings: list[dict[str, Any]] = []

        if not healthy:
            findings.append(
                {
                    "code": "database_health_check_failed",
                    "severity": "error",
                    "summary": "PostgreSQL health check failed.",
                    "evidence": [
                        "The PostgreSQL health check did not succeed."
                    ],
                    "recommendation": (
                        "Check PostgreSQL availability, connectivity, "
                        "configuration, and authentication."
                    ),
                }
            )

        if not tables:
            findings.append(
                {
                    "code": "no_public_tables",
                    "severity": "warning",
                    "summary": "No public tables were found.",
                    "evidence": [
                        "The public schema contains no base tables."
                    ],
                    "recommendation": (
                        "Verify that the expected database and schema "
                        "are being inspected."
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
            "system": "postgresql",
            "resource": {
                "kind": "Database",
            },
            "findings": findings,
        }
