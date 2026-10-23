"""Deterministic diagnostics for Kubernetes observations."""

from typing import Any


class KubernetesDiagnostics:
    """Analyze Kubernetes observations and produce structured findings."""

    def diagnose_pod(
        self,
        pod: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Diagnose one Pod from its summary and related Kubernetes events."""
        findings: list[dict[str, Any]] = []

        if pod["phase"] != "Running":
            findings.append(
                {
                    "code": "pod_not_running",
                    "severity": "warning",
                    "summary": "Pod is not running.",
                    "evidence": [
                        f"Pod phase is {pod['phase']}."
                    ],
                    "recommendation": (
                        "Inspect the Pod state and Kubernetes events "
                        "to determine why it is not running."
                    ),
                }
            )

        if not pod["ready"]:
            findings.append(
                {
                    "code": "pod_not_ready",
                    "severity": "warning",
                    "summary": "Pod is not ready.",
                    "evidence": [
                        "Pod readiness is false."
                    ],
                    "recommendation": (
                        "Inspect container readiness and Pod events."
                    ),
                }
            )

        if pod["restarts"] > 0:
            findings.append(
                {
                    "code": "container_restarts",
                    "severity": "warning",
                    "summary": "Pod containers have restarted.",
                    "evidence": [
                        f"Pod has {pod['restarts']} container restart(s)."
                    ],
                    "recommendation": (
                        "Inspect container logs and events for the "
                        "cause of the restarts."
                    ),
                }
            )

        image_pull_evidence: list[str] = []

        for event in events:
            message = event.get("message") or ""
            event_type = event.get("type") or ""
            reason = event.get("reason") or ""

            if "ImagePullBackOff" in message or "ErrImagePull" in message:
                if message not in image_pull_evidence:
                    image_pull_evidence.append(message)

            elif "CrashLoopBackOff" in message:
                findings.append(
                    {
                        "code": "container_crash_loop",
                        "severity": "error",
                        "summary": "Container is repeatedly crashing.",
                        "evidence": [message],
                        "recommendation": (
                            "Inspect the container logs, command, "
                            "configuration, and dependencies."
                        ),
                    }
                )

            elif event_type == "Warning":
                findings.append(
                    {
                        "code": "kubernetes_warning_event",
                        "severity": "warning",
                        "summary": "Kubernetes reported a warning event.",
                        "evidence": [
                            f"{reason}: {message}"
                        ],
                        "recommendation": (
                            "Inspect the warning event and affected "
                            "Kubernetes resource."
                        ),
                    }
                )

        if image_pull_evidence:
            findings.append(
                {
                    "code": "image_pull_failure",
                    "severity": "error",
                    "summary": "Container image could not be pulled.",
                    "evidence": image_pull_evidence,
                    "recommendation": (
                        "Verify the image name, tag, registry "
                        "availability, and registry credentials."
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
            "system": "kubernetes",
            "resource": {
                "kind": "Pod",
                "name": pod["name"],
                "namespace": pod["namespace"],
            },
            "findings": findings,
        }
