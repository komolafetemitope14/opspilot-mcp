"""Small synchronous, read-only Kubernetes client configured through kubeconfig."""

from typing import Any

from kubernetes import client, config


from opspilot.redaction import register_kubernetes_credentials


class KubernetesClient:
    def __init__(self) -> None:
        config.load_kube_config()
        self._client = client.CoreV1Api()
        configuration = self._client.api_client.configuration
        register_kubernetes_credentials(configuration)
        refresh = configuration.refresh_api_key_hook
        if refresh is not None:
            def register_refresh(current):
                try:
                    refresh(current)
                finally:
                    register_kubernetes_credentials(current)
            configuration.refresh_api_key_hook = register_refresh

    def health_check(self) -> bool:
        """Request core API discovery; configuration and API errors propagate."""
        self._client.get_api_resources()
        return True

    def list_namespaces(self) -> list[str]:
        """Return sorted namespace names."""
        namespaces = self._client.list_namespace()
        return sorted(namespace.metadata.name for namespace in namespaces.items)

    def list_pods(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Return Pod summaries sorted by namespace and name."""
        if namespace is None:
            pods = self._client.list_pod_for_all_namespaces()
        else:
            pods = self._client.list_namespaced_pod(namespace)
        summaries = [self._pod_summary(pod) for pod in pods.items]
        return sorted(summaries, key=lambda pod: (pod["namespace"], pod["name"]))

    def inspect_pod(self, name: str, namespace: str = "default") -> dict[str, Any]:
        """Return a summary for the named Pod."""
        pod = self._client.read_namespaced_pod(name, namespace)
        return self._pod_summary(pod)

    def list_events(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Return curated Event summaries in deterministic order."""
        if namespace is None:
            events = self._client.list_event_for_all_namespaces()
        else:
            events = self._client.list_namespaced_event(namespace)
        summaries = [
            {
                "namespace": event.metadata.namespace,
                "kind": event.involved_object.kind,
                "name": event.involved_object.name,
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "count": event.count,
                "first_timestamp": (
                    event.first_timestamp.isoformat() if event.first_timestamp else None
                ),
                "last_timestamp": (
                    event.last_timestamp.isoformat() if event.last_timestamp else None
                ),
            }
            for event in events.items
        ]
        return sorted(summaries, key=lambda event: (
            event["namespace"], event["kind"], event["name"],
            event["last_timestamp"] or "", event["reason"] or "",
        ))

    def pod_logs(
        self, name: str, namespace: str = "default", tail: int = 100
    ) -> str:
        """Return the named Pod's log text, limited to the requested tail."""
        if isinstance(tail, bool) or not isinstance(tail, int) or tail <= 0:
            raise ValueError("tail must be a positive integer")
        return self._client.read_namespaced_pod_log(name, namespace, tail_lines=tail)

    @staticmethod
    def _pod_summary(pod: client.V1Pod) -> dict[str, Any]:
        statuses = pod.status.container_statuses or []
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase,
            "ready": bool(statuses) and all(status.ready is True for status in statuses),
            "restarts": sum(status.restart_count for status in statuses),
            "node": pod.spec.node_name,
            "pod_ip": pod.status.pod_ip,
        }
