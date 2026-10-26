"""Small synchronous, read-only Kubernetes client configured through kubeconfig."""

from typing import Any
from functools import wraps

from urllib3.exceptions import TimeoutError as HTTPTimeoutError

from kubernetes import client, config


from opspilot.configuration import (
    ConfigurationError, KubernetesSettings, load_kubernetes_settings,
)
from opspilot.redaction import install_kubernetes_registration
from opspilot.limits import (
    KUBERNETES_CONNECT_TIMEOUT_SECONDS, KUBERNETES_READ_TIMEOUT_SECONDS,
    MAX_LIST_ITEMS, LIST_PAGE_SIZE, DEFAULT_LOG_TAIL, ObservationBudget,
    ObservationError, ObservationLimitError, ObservationTimeoutError,
    checked_observation, validate_tail, check_log,
)

REQUEST_TIMEOUT = (KUBERNETES_CONNECT_TIMEOUT_SECONDS, KUBERNETES_READ_TIMEOUT_SECONDS)


def _close_after_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except HTTPTimeoutError:
            raise ObservationTimeoutError() from None
        finally:
            self.close()
    return wrapped


class KubernetesClient:
    def __init__(self, configuration: KubernetesSettings | None = None) -> None:
        settings = configuration if configuration is not None else load_kubernetes_settings()
        effective = client.Configuration()
        try:
            config.load_kube_config_from_dict(
                settings.kubeconfig(), client_configuration=effective, persist_config=False)
        except Exception:
            raise ConfigurationError("kubeconfig static configuration could not be loaded") from None
        install_kubernetes_registration(effective)
        effective.retries = 0
        self._api_client = client.ApiClient(configuration=effective)
        try:
            self._client = client.CoreV1Api(self._api_client)
        except BaseException:
            self.close()
            raise

    def close(self):
        # SDK ApiClient.close() only joins its async thread pool; it does not
        # clear urllib3's connection pools. The pool manager remains reusable.
        try:
            self._api_client.close()
        finally:
            self._api_client.rest_client.pool_manager.clear()

    def _list(self, method, project, *args, **kwargs):
        # Each SDK page is fully buffered before projection. A page/item count
        # is not a byte or memory bound. Only bounded projections are retained.
        summaries = []
        budget = ObservationBudget().start_list()
        token = None
        seen = set()
        # Also bound empty/short pages with endlessly changing tokens.
        for _ in range(MAX_LIST_ITEMS + 1):
            params = dict(kwargs, limit=LIST_PAGE_SIZE, _request_timeout=REQUEST_TIMEOUT)
            if token:
                params["_continue"] = token
            page = method(*args, **params)
            for item in page.items:
                if len(summaries) >= MAX_LIST_ITEMS:
                    raise ObservationLimitError()
                summary = project(item)
                budget.add_item(summary)
                summaries.append(summary)
            token = page.metadata._continue
            if not token:
                return summaries
            if not isinstance(token, str) or token in seen:
                raise ObservationError("Observation pagination did not progress")
            seen.add(token)
        raise ObservationLimitError()

    @_close_after_operation
    def health_check(self) -> bool:
        """Request core API discovery with a native connect/read timeout."""
        self._client.get_api_resources(_request_timeout=REQUEST_TIMEOUT)
        return True

    @_close_after_operation
    def list_namespaces(self) -> list[str]:
        return sorted(self._list(self._client.list_namespace,
                                 lambda item: item.metadata.name))

    @_close_after_operation
    def list_pods(self, namespace: str | None = None) -> list[dict[str, Any]]:
        if namespace is None:
            summaries = self._list(self._client.list_pod_for_all_namespaces, self._pod_summary)
        else:
            summaries = self._list(self._client.list_namespaced_pod, self._pod_summary, namespace)
        return sorted(summaries, key=lambda pod: (pod["namespace"], pod["name"]))

    @_close_after_operation
    def inspect_pod(self, name: str, namespace: str = "default") -> dict[str, Any]:
        pod = self._client.read_namespaced_pod(name, namespace, _request_timeout=REQUEST_TIMEOUT)
        return checked_observation(self._pod_summary(pod))

    @_close_after_operation
    def list_events(self, namespace: str | None = None, *,
                    pod_name: str | None = None) -> list[dict[str, Any]]:
        params = {}
        if pod_name is not None:
            # A DNS-subdomain Pod name cannot inject another selector term.
            import re
            if (not isinstance(pod_name, str) or len(pod_name) > 253
                    or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", pod_name) is None):
                raise ObservationError("Invalid Pod name for event observation")
            params["field_selector"] = f"involvedObject.kind=Pod,involvedObject.name={pod_name}"
        if namespace is None:
            summaries = self._list(self._client.list_event_for_all_namespaces,
                                   self._event_summary, **params)
        else:
            summaries = self._list(self._client.list_namespaced_event,
                                   self._event_summary, namespace, **params)
        return sorted(summaries, key=lambda event: (
            event["namespace"], event["kind"], event["name"],
            event["last_timestamp"] or "", event["reason"] or "",
        ))

    @staticmethod
    def _event_summary(event):
        return {
            "namespace": event.metadata.namespace,
            "kind": event.involved_object.kind,
            "name": event.involved_object.name,
            "type": event.type,
            "reason": event.reason,
            "message": event.message,
            "count": event.count,
            "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
            "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
        }

    @_close_after_operation
    def pod_logs(self, name: str, namespace: str = "default",
                 tail: int = DEFAULT_LOG_TAIL) -> str:
        validate_tail(tail)
        # The SDK has buffered/decoded this response already. Tail is a line
        # count, not a byte cap. No claim of a hard transport memory bound.
        return check_log(self._client.read_namespaced_pod_log(
            name, namespace, tail_lines=tail, follow=False,
            _request_timeout=REQUEST_TIMEOUT))

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
