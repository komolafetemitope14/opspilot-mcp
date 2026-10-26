from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from opspilot.clients import kubernetes


@pytest.fixture
def cluster(monkeypatch):
    sdk = MagicMock()
    sdk.CoreV1Api.return_value.api_client.configuration = SimpleNamespace(
        password=None, api_key={}, refresh_api_key_hook=None)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", sdk.load_kube_config)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", sdk.CoreV1Api)
    return sdk


def test_construction_loads_kubeconfig_before_creating_api(cluster):
    kubernetes.KubernetesClient()
    assert cluster.mock_calls == [call.load_kube_config(), call.CoreV1Api()]


def test_configuration_errors_propagate(cluster):
    error = ConfigException("invalid kubeconfig")
    cluster.load_kube_config.side_effect = error
    with pytest.raises(ConfigException) as caught:
        kubernetes.KubernetesClient()
    assert caught.value is error
    cluster.CoreV1Api.assert_not_called()


def test_api_construction_errors_propagate(cluster):
    error = ApiException(status=500, reason="unavailable")
    cluster.CoreV1Api.side_effect = error
    with pytest.raises(ApiException) as caught:
        kubernetes.KubernetesClient()
    assert caught.value is error


def test_health_check_requests_api_discovery(cluster):
    api = cluster.CoreV1Api.return_value
    api.get_api_resources.return_value = SimpleNamespace()
    assert kubernetes.KubernetesClient().health_check() is True
    api.get_api_resources.assert_called_once_with()
    api.list_namespace.assert_not_called()


@pytest.mark.parametrize("names", [[], ["default"], ["zeta", "default", "alpha"]])
def test_list_namespaces_returns_sorted_strings(cluster, names):
    api = cluster.CoreV1Api.return_value
    api.list_namespace.return_value = SimpleNamespace(items=[
        SimpleNamespace(metadata=SimpleNamespace(name=name)) for name in names
    ])
    result = kubernetes.KubernetesClient().list_namespaces()
    assert result == sorted(names)
    assert all(isinstance(name, str) for name in result)
    api.list_namespace.assert_called_once_with()


@pytest.mark.parametrize("method, sdk_method", [
    ("health_check", "get_api_resources"),
    ("list_namespaces", "list_namespace"),
])
def test_api_errors_propagate(cluster, method, sdk_method):
    error = ApiException(status=403, reason="forbidden")
    getattr(cluster.CoreV1Api.return_value, sdk_method).side_effect = error
    with pytest.raises(ApiException) as caught:
        getattr(kubernetes.KubernetesClient(), method)()
    assert caught.value is error


def make_pod(name="web", namespace="default", statuses=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        spec=SimpleNamespace(node_name="worker-1"),
        status=SimpleNamespace(
            phase="Running", pod_ip="10.0.0.2", container_statuses=statuses
        ),
    )


def test_list_pods_all_namespaces_sorted(cluster):
    api = cluster.CoreV1Api.return_value
    api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[
        make_pod("alpha", "zeta"),
        make_pod("zeta", "alpha"),
        make_pod("alpha", "alpha"),
    ])
    result = kubernetes.KubernetesClient().list_pods()
    assert [(pod["namespace"], pod["name"]) for pod in result] == [
        ("alpha", "alpha"), ("alpha", "zeta"), ("zeta", "alpha")
    ]
    api.list_pod_for_all_namespaces.assert_called_once_with()
    api.list_namespaced_pod.assert_not_called()


@pytest.mark.parametrize("namespace", ["default", "production"])
def test_list_pods_in_namespace(cluster, namespace):
    api = cluster.CoreV1Api.return_value
    api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[make_pod(namespace=namespace)]
    )
    result = kubernetes.KubernetesClient().list_pods(namespace)
    assert len(result) == 1
    assert result[0]["namespace"] == namespace
    api.list_namespaced_pod.assert_called_once_with(namespace)
    api.list_pod_for_all_namespaces.assert_not_called()


@pytest.mark.parametrize("namespace", [None, "default"])
def test_list_pods_empty(cluster, namespace):
    api = cluster.CoreV1Api.return_value
    api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])
    api.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    assert kubernetes.KubernetesClient().list_pods(namespace) == []


@pytest.mark.parametrize("method", ["list", "inspect"])
@pytest.mark.parametrize("statuses, ready, restarts", [
    (None, False, 0),
    ([], False, 0),
    ([SimpleNamespace(ready=True, restart_count=2),
      SimpleNamespace(ready=True, restart_count=3)], True, 5),
    ([SimpleNamespace(ready=True, restart_count=2),
      SimpleNamespace(ready=False, restart_count=4)], False, 6),
    ([SimpleNamespace(ready=None, restart_count=0)], False, 0),
])
def test_pod_summary(cluster, method, statuses, ready, restarts):
    import json

    api = cluster.CoreV1Api.return_value
    pod = make_pod(statuses=statuses)
    api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[pod])
    api.read_namespaced_pod.return_value = pod
    client = kubernetes.KubernetesClient()
    result = client.list_pods()[0] if method == "list" else client.inspect_pod("web")
    assert result == {
        "name": "web", "namespace": "default", "phase": "Running",
        "ready": ready, "restarts": restarts, "node": "worker-1",
        "pod_ip": "10.0.0.2",
    }
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize("namespace", [None, "production"])
def test_inspect_pod_name_and_namespace(cluster, namespace):
    api = cluster.CoreV1Api.return_value
    expected_namespace = "default" if namespace is None else namespace
    api.read_namespaced_pod.return_value = make_pod("api", expected_namespace)
    client = kubernetes.KubernetesClient()
    result = (client.inspect_pod("api") if namespace is None
              else client.inspect_pod("api", namespace))
    api.read_namespaced_pod.assert_called_once_with("api", expected_namespace)
    assert result["name"] == "api"
    assert result["namespace"] == expected_namespace


@pytest.mark.parametrize("method, args, sdk_method", [
    ("list_pods", (), "list_pod_for_all_namespaces"),
    ("list_pods", ("default",), "list_namespaced_pod"),
    ("inspect_pod", ("web", "production"), "read_namespaced_pod"),
])
def test_pod_api_errors_propagate(cluster, method, args, sdk_method):
    error = ApiException(status=403, reason="forbidden")
    getattr(cluster.CoreV1Api.return_value, sdk_method).side_effect = error
    with pytest.raises(ApiException) as caught:
        getattr(kubernetes.KubernetesClient(), method)(*args)
    assert caught.value is error


def test_pod_logs_defaults(cluster):
    api = cluster.CoreV1Api.return_value
    logs = "  first line\nsecond line\n\n"
    api.read_namespaced_pod_log.return_value = logs

    assert kubernetes.KubernetesClient().pod_logs("web") is logs
    api.read_namespaced_pod_log.assert_called_once_with(
        "web", "default", tail_lines=100
    )


def test_pod_logs_explicit_namespace_and_tail(cluster):
    api = cluster.CoreV1Api.return_value
    api.read_namespaced_pod_log.return_value = ""

    assert kubernetes.KubernetesClient().pod_logs("api", "production", 1) == ""
    api.read_namespaced_pod_log.assert_called_once_with(
        "api", "production", tail_lines=1
    )


@pytest.mark.parametrize("tail", [0, -1, -100, 1.0, 0.5, "100", None, [], {}, True, False])
def test_pod_logs_rejects_invalid_tail_before_api_call(cluster, tail):
    client = kubernetes.KubernetesClient()
    api = cluster.CoreV1Api.return_value

    with pytest.raises(ValueError, match="^tail must be a positive integer$"):
        client.pod_logs("web", tail=tail)

    assert api.mock_calls == []


def test_pod_logs_api_exception_propagates_unchanged(cluster):
    api = cluster.CoreV1Api.return_value
    error = ApiException(status=403, reason="forbidden")
    api.read_namespaced_pod_log.side_effect = error

    with pytest.raises(ApiException) as caught:
        kubernetes.KubernetesClient().pod_logs("web")

    assert caught.value is error
    api.read_namespaced_pod_log.assert_called_once_with(
        "web", "default", tail_lines=100
    )


def make_event(namespace="default", kind="Pod", name="web", reason="Started",
               first_timestamp=None, last_timestamp=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace, name="event-name"),
        involved_object=SimpleNamespace(kind=kind, name=name),
        type="Normal", reason=reason, message="Started container", count=2,
        first_timestamp=first_timestamp, last_timestamp=last_timestamp,
        extra="must not appear",
    )


@pytest.mark.parametrize("namespace", [None, "production", ""])
def test_list_events_uses_requested_scope(cluster, namespace):
    api = cluster.CoreV1Api.return_value
    sdk_method = (api.list_event_for_all_namespaces if namespace is None
                  else api.list_namespaced_event)
    event_namespace = "default" if namespace is None else namespace
    sdk_method.return_value = SimpleNamespace(items=[
        make_event(namespace=event_namespace)
    ])

    result = kubernetes.KubernetesClient().list_events(namespace)

    assert len(result) == 1
    assert result[0]["namespace"] == event_namespace
    if namespace is None:
        api.list_event_for_all_namespaces.assert_called_once_with()
        api.list_namespaced_event.assert_not_called()
    else:
        api.list_namespaced_event.assert_called_once_with(namespace)
        api.list_event_for_all_namespaces.assert_not_called()


@pytest.mark.parametrize("timestamps_present", [False, True])
def test_list_events_curated_json_summary(cluster, timestamps_present):
    first = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    last = datetime(2026, 1, 2, 3, 5, 6, 123456, tzinfo=timezone.utc)
    api = cluster.CoreV1Api.return_value
    api.list_event_for_all_namespaces.return_value = SimpleNamespace(items=[
        make_event(first_timestamp=first if timestamps_present else None,
                   last_timestamp=last if timestamps_present else None)
    ])

    result = kubernetes.KubernetesClient().list_events()

    assert result == [{
        "namespace": "default", "kind": "Pod", "name": "web",
        "type": "Normal", "reason": "Started", "message": "Started container",
        "count": 2,
        "first_timestamp": "2026-01-02T03:04:05+00:00" if timestamps_present else None,
        "last_timestamp": "2026-01-02T03:05:06.123456+00:00" if timestamps_present else None,
    }]
    assert json.loads(json.dumps(result)) == result


def test_list_events_deterministic_sorting(cluster):
    early = datetime(2026, 1, 1, tzinfo=timezone.utc)
    late = datetime(2026, 1, 2, tzinfo=timezone.utc)
    expected = [
        make_event(reason=None),
        make_event(reason="Alpha"),
        make_event(reason="Zeta"),
        make_event(last_timestamp=early),
        make_event(last_timestamp=late, reason="Alpha"),
        make_event(last_timestamp=late, reason="Zeta"),
        make_event(name="zeta"),
        make_event(kind="Service", name="alpha"),
        make_event(namespace="zeta", kind="Deployment", name="alpha"),
    ]
    api = cluster.CoreV1Api.return_value
    client = kubernetes.KubernetesClient()
    for events in (list(reversed(expected)), expected[3:] + expected[:3]):
        api.list_event_for_all_namespaces.return_value = SimpleNamespace(items=events)
        result = client.list_events()
        assert [
            (event["namespace"], event["kind"], event["name"],
             event["last_timestamp"], event["reason"])
            for event in result
        ] == [
            (event.metadata.namespace, event.involved_object.kind,
             event.involved_object.name,
             event.last_timestamp.isoformat() if event.last_timestamp else None,
             event.reason)
            for event in expected
        ]


@pytest.mark.parametrize("namespace", [None, "production"])
def test_list_events_empty(cluster, namespace):
    api = cluster.CoreV1Api.return_value
    api.list_event_for_all_namespaces.return_value = SimpleNamespace(items=[])
    api.list_namespaced_event.return_value = SimpleNamespace(items=[])
    assert kubernetes.KubernetesClient().list_events(namespace) == []


@pytest.mark.parametrize("namespace, sdk_method", [
    (None, "list_event_for_all_namespaces"),
    ("production", "list_namespaced_event"),
])
def test_list_events_api_exception_propagates_unchanged(cluster, namespace, sdk_method):
    error = ApiException(status=403, reason="forbidden")
    getattr(cluster.CoreV1Api.return_value, sdk_method).side_effect = error

    with pytest.raises(ApiException) as caught:
        kubernetes.KubernetesClient().list_events(namespace)

    assert caught.value is error


def test_effective_credentials_registered_and_refreshed(cluster, monkeypatch):
    from opspilot.redaction import registry, redactor
    configuration = cluster.CoreV1Api.return_value.api_client.configuration
    configuration.password = "kube-configured-password"
    configuration.api_key = {"authorization": "Bearer kube-original-secret"}

    def refresh(current):
        current.api_key["authorization"] = "Bearer kube-refreshed-secret"
    configuration.refresh_api_key_hook = refresh
    kubernetes.KubernetesClient()
    assert redactor.redact_text("kube-configured-password kube-original-secret") == "[REDACTED] [REDACTED]"
    configuration.refresh_api_key_hook(configuration)
    assert redactor.redact_text("kube-refreshed-secret") == "[REDACTED]"
