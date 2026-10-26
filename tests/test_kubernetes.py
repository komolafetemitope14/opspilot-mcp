from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from opspilot.clients import kubernetes
from opspilot.clients.kubernetes import REQUEST_TIMEOUT


class Page(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(metadata=SimpleNamespace(_continue=None), **kwargs)


@pytest.fixture
def cluster(monkeypatch):
    sdk = MagicMock()
    sdk.CoreV1Api.return_value.api_client.configuration = SimpleNamespace(
        password=None, api_key={}, refresh_api_key_hook=None)
    monkeypatch.setattr(kubernetes, "load_kubernetes_settings", MagicMock())
    monkeypatch.setattr(kubernetes.config, "load_kube_config_from_dict", sdk.load_kube_config)
    monkeypatch.setattr(kubernetes.client, "Configuration", lambda: sdk.CoreV1Api.return_value.api_client.configuration)
    monkeypatch.setattr(kubernetes.client, "ApiClient", sdk.ApiClient)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", sdk.CoreV1Api)
    return sdk


def test_construction_loads_kubeconfig_before_creating_api(cluster):
    kubernetes.KubernetesClient()
    cluster.load_kube_config.assert_called_once()
    assert cluster.load_kube_config.call_args.kwargs["persist_config"] is False
    effective = cluster.load_kube_config.call_args.kwargs["client_configuration"]
    cluster.ApiClient.assert_called_once_with(configuration=effective)
    cluster.CoreV1Api.assert_called_once_with(cluster.ApiClient.return_value)


def test_configuration_errors_propagate(cluster):
    error = ConfigException("invalid kubeconfig")
    cluster.load_kube_config.side_effect = error
    with pytest.raises(kubernetes.ConfigurationError) as caught:
        kubernetes.KubernetesClient()
    assert "invalid kubeconfig" not in str(caught.value)
    assert caught.value.__suppress_context__
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
    api.get_api_resources.assert_called_once_with(_request_timeout=REQUEST_TIMEOUT)
    api.list_namespace.assert_not_called()


@pytest.mark.parametrize("names", [[], ["default"], ["zeta", "default", "alpha"]])
def test_list_namespaces_returns_sorted_strings(cluster, names):
    api = cluster.CoreV1Api.return_value
    api.list_namespace.return_value = Page(items=[
        SimpleNamespace(metadata=SimpleNamespace(name=name)) for name in names
    ])
    result = kubernetes.KubernetesClient().list_namespaces()
    assert result == sorted(names)
    assert all(isinstance(name, str) for name in result)
    api.list_namespace.assert_called_once_with(limit=100, _request_timeout=REQUEST_TIMEOUT)


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
    api.list_pod_for_all_namespaces.return_value = Page(items=[
        make_pod("alpha", "zeta"),
        make_pod("zeta", "alpha"),
        make_pod("alpha", "alpha"),
    ])
    result = kubernetes.KubernetesClient().list_pods()
    assert [(pod["namespace"], pod["name"]) for pod in result] == [
        ("alpha", "alpha"), ("alpha", "zeta"), ("zeta", "alpha")
    ]
    api.list_pod_for_all_namespaces.assert_called_once_with(limit=100, _request_timeout=REQUEST_TIMEOUT)
    api.list_namespaced_pod.assert_not_called()


@pytest.mark.parametrize("namespace", ["default", "production"])
def test_list_pods_in_namespace(cluster, namespace):
    api = cluster.CoreV1Api.return_value
    api.list_namespaced_pod.return_value = Page(
        items=[make_pod(namespace=namespace)]
    )
    result = kubernetes.KubernetesClient().list_pods(namespace)
    assert len(result) == 1
    assert result[0]["namespace"] == namespace
    api.list_namespaced_pod.assert_called_once_with(namespace, limit=100, _request_timeout=REQUEST_TIMEOUT)
    api.list_pod_for_all_namespaces.assert_not_called()


@pytest.mark.parametrize("namespace", [None, "default"])
def test_list_pods_empty(cluster, namespace):
    api = cluster.CoreV1Api.return_value
    api.list_pod_for_all_namespaces.return_value = Page(items=[])
    api.list_namespaced_pod.return_value = Page(items=[])
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
    api.list_pod_for_all_namespaces.return_value = Page(items=[pod])
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
    api.read_namespaced_pod.assert_called_once_with("api", expected_namespace, _request_timeout=REQUEST_TIMEOUT)
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
        "web", "default", tail_lines=100, follow=False, _request_timeout=REQUEST_TIMEOUT
    )


def test_pod_logs_explicit_namespace_and_tail(cluster):
    api = cluster.CoreV1Api.return_value
    api.read_namespaced_pod_log.return_value = ""

    assert kubernetes.KubernetesClient().pod_logs("api", "production", 1) == ""
    api.read_namespaced_pod_log.assert_called_once_with(
        "api", "production", tail_lines=1, follow=False, _request_timeout=REQUEST_TIMEOUT
    )


@pytest.mark.parametrize("tail", [0, -1, -100, 1001, 1.0, 0.5, "100", None, [], {}, True, False])
def test_pod_logs_rejects_invalid_tail_before_api_call(cluster, tail):
    client = kubernetes.KubernetesClient()
    api = cluster.CoreV1Api.return_value

    with pytest.raises(ValueError, match="^tail must be an integer between 1 and 1000$"):
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
        "web", "default", tail_lines=100, follow=False, _request_timeout=REQUEST_TIMEOUT
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
    sdk_method.return_value = Page(items=[
        make_event(namespace=event_namespace)
    ])

    result = kubernetes.KubernetesClient().list_events(namespace)

    assert len(result) == 1
    assert result[0]["namespace"] == event_namespace
    if namespace is None:
        api.list_event_for_all_namespaces.assert_called_once_with(limit=100, _request_timeout=REQUEST_TIMEOUT)
        api.list_namespaced_event.assert_not_called()
    else:
        api.list_namespaced_event.assert_called_once_with(namespace, limit=100, _request_timeout=REQUEST_TIMEOUT)
        api.list_event_for_all_namespaces.assert_not_called()


@pytest.mark.parametrize("timestamps_present", [False, True])
def test_list_events_curated_json_summary(cluster, timestamps_present):
    first = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    last = datetime(2026, 1, 2, 3, 5, 6, 123456, tzinfo=timezone.utc)
    api = cluster.CoreV1Api.return_value
    api.list_event_for_all_namespaces.return_value = Page(items=[
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
        api.list_event_for_all_namespaces.return_value = Page(items=events)
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
    api.list_event_for_all_namespaces.return_value = Page(items=[])
    api.list_namespaced_event.return_value = Page(items=[])
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


def test_validated_static_snapshot_uses_explicit_real_sdk_configuration(monkeypatch, tmp_path):
    from opspilot.configuration import load_kubernetes_settings
    path = tmp_path / "config"
    path.write_text(json.dumps({
        "current-context": "reader",
        "contexts": [{"name": "reader", "context": {"cluster": "cluster", "user": "reader"}}],
        "clusters": [{"name": "cluster", "cluster": {"server": "https://synthetic.example"}}],
        "users": [{"name": "reader", "user": {"token": "synthetic-sdk-token"}}],
    }))
    settings = load_kubernetes_settings({}, path)
    api_client = MagicMock()
    core = MagicMock()
    monkeypatch.setattr(kubernetes.client, "ApiClient", api_client)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", core)
    default = kubernetes.client.Configuration._default
    kubernetes.KubernetesClient(settings)
    effective = api_client.call_args.kwargs["configuration"]
    assert effective.host == "https://synthetic.example"
    assert effective.verify_ssl is True
    assert effective.api_key["authorization"] == "Bearer synthetic-sdk-token"
    assert kubernetes.client.Configuration._default is default
    core.assert_called_once_with(api_client.return_value)
    assert core.return_value.method_calls == []


def test_configuration_failure_prevents_sdk_activation(monkeypatch):
    loader = MagicMock(side_effect=kubernetes.ConfigurationError("kubeconfig is invalid"))
    forbidden = MagicMock(side_effect=AssertionError("Must not activate Kubernetes"))
    monkeypatch.setattr(kubernetes, "load_kubernetes_settings", loader)
    monkeypatch.setattr(kubernetes.config, "load_kube_config_from_dict", forbidden)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", forbidden)
    for name in ("Configuration", "ApiClient", "CoreV1Api"):
        monkeypatch.setattr(kubernetes.client, name, forbidden)
    with pytest.raises(kubernetes.ConfigurationError):
        kubernetes.KubernetesClient()
    loader.assert_called_once_with()
    forbidden.assert_not_called()


@pytest.mark.parametrize("mutation", ["change", "delete"])
def test_activation_uses_snapshot_after_source_material_changes(monkeypatch, tmp_path, mutation):
    import base64
    import socket
    import subprocess
    from pathlib import Path
    from opspilot.configuration import load_kubernetes_settings

    forbidden = MagicMock(side_effect=AssertionError("Must not contact infrastructure"))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    material = {"token": "snapshot-token", "ca": "snapshot-ca",
                "cert": "snapshot-certificate", "key": "snapshot-private-key"}
    for name, value in material.items():
        (tmp_path / name).write_text(value)
    path = tmp_path / "config"
    path.write_text(json.dumps({
        "current-context": "reader",
        "contexts": [{"name": "reader", "context": {"cluster": "cluster", "user": "reader"}}],
        "clusters": [{"name": "cluster", "cluster": {
            "server": "https://snapshot.example", "certificate-authority": "ca"}}],
        "users": [{"name": "reader", "user": {
            "tokenFile": "token", "client-certificate": "cert", "client-key": "key"}}],
    }))
    settings = load_kubernetes_settings({}, path)
    for original in [path, *(tmp_path / name for name in material)]:
        if mutation == "delete":
            original.unlink()
        else:
            original.write_text("changed-after-validation")
    api_client, core = MagicMock(), MagicMock()
    monkeypatch.setattr(kubernetes.client, "ApiClient", api_client)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", core)
    kubernetes.KubernetesClient(settings)
    effective = api_client.call_args.kwargs["configuration"]
    assert effective.host == "https://snapshot.example"
    assert effective.api_key["authorization"] == "Bearer snapshot-token"
    assert effective.verify_ssl is True
    for name, sdk_path in (("ca", effective.ssl_ca_cert),
                           ("cert", effective.cert_file), ("key", effective.key_file)):
        assert Path(sdk_path).read_text() == material[name]
        assert Path(sdk_path) != tmp_path / name
    assert settings.kubeconfig()["users"][0]["user"]["client-key-data"] == base64.b64encode(
        material["key"].encode()).decode()
    core.assert_called_once_with(api_client.return_value)
    assert core.return_value.method_calls == []
    forbidden.assert_not_called()


def paged(items, token=None):
    return SimpleNamespace(items=items, metadata=SimpleNamespace(_continue=token))


def test_retries_disabled(cluster):
    kubernetes.KubernetesClient()
    assert cluster.ApiClient.call_args.kwargs['configuration'].retries == 0


@pytest.mark.parametrize('method,sdk_method,args,item', [
    ('list_namespaces', 'list_namespace', (), SimpleNamespace(metadata=SimpleNamespace(name='default'))),
    ('list_pods', 'list_pod_for_all_namespaces', (), make_pod()),
    ('list_pods', 'list_namespaced_pod', ('default',), make_pod()),
    ('list_events', 'list_event_for_all_namespaces', (), make_event()),
    ('list_events', 'list_namespaced_event', ('default',), make_event()),
])
@pytest.mark.parametrize('count', [500, 501])
def test_paginated_item_boundaries(cluster, method, sdk_method, args, item, count):
    from opspilot.limits import ObservationLimitError
    api_method = getattr(cluster.CoreV1Api.return_value, sdk_method)
    pages = [paged([item] * min(100, count - n), str(n + 100) if n + 100 < count else None)
             for n in range(0, count, 100)]
    api_method.side_effect = pages
    instance = kubernetes.KubernetesClient()
    if count == 500:
        assert len(getattr(instance, method)(*args)) == 500
    else:
        with pytest.raises(ObservationLimitError):
            getattr(instance, method)(*args)
    for call_args in api_method.call_args_list:
        assert call_args.kwargs['limit'] == 100
        assert call_args.kwargs['_request_timeout'] == (5, 10)
    assert api_method.call_count == len(pages)
    cluster.ApiClient.return_value.close.assert_called_once()
    cluster.ApiClient.return_value.rest_client.pool_manager.clear.assert_called_once()


def test_short_and_empty_pages_with_continue_do_not_end_listing(cluster):
    api = cluster.CoreV1Api.return_value
    api.list_namespace.side_effect = [
        paged([SimpleNamespace(metadata=SimpleNamespace(name='z'))], 'first'),
        paged([], 'second'),
        paged([SimpleNamespace(metadata=SimpleNamespace(name='a'))]),
    ]
    assert kubernetes.KubernetesClient().list_namespaces() == ['a', 'z']
    assert api.list_namespace.call_args_list == [
        call(limit=100, _request_timeout=REQUEST_TIMEOUT),
        call(limit=100, _request_timeout=REQUEST_TIMEOUT, _continue='first'),
        call(limit=100, _request_timeout=REQUEST_TIMEOUT, _continue='second'),
    ]


def test_repeated_token_fails_and_closes(cluster):
    from opspilot.limits import ObservationError
    api = cluster.CoreV1Api.return_value
    api.list_namespace.side_effect = [paged([], 'stuck'), paged([], 'stuck')]
    with pytest.raises(ObservationError, match='pagination did not progress'):
        kubernetes.KubernetesClient().list_namespaces()
    assert api.list_namespace.call_count == 2
    cluster.ApiClient.return_value.close.assert_called_once()


def test_endless_unique_empty_pages_are_bounded(cluster):
    from opspilot.limits import ObservationLimitError
    api = cluster.CoreV1Api.return_value
    api.list_namespace.side_effect = (paged([], str(n)) for n in range(1000))
    with pytest.raises(ObservationLimitError):
        kubernetes.KubernetesClient().list_namespaces()
    assert api.list_namespace.call_count == 501


def test_pod_event_selector_survives_pagination(cluster):
    api = cluster.CoreV1Api.return_value
    api.list_namespaced_event.side_effect = [paged([make_event()], 'next'), paged([])]
    assert len(kubernetes.KubernetesClient().list_events('default', pod_name='web')) == 1
    for args in api.list_namespaced_event.call_args_list:
        assert args.kwargs['field_selector'] == 'involvedObject.kind=Pod,involvedObject.name=web'
        assert args.kwargs['_request_timeout'] == REQUEST_TIMEOUT


@pytest.mark.parametrize('name', ['web,metadata.name=other', '', '/web'])
def test_selector_input_cannot_inject_terms(cluster, name):
    from opspilot.limits import ObservationError
    with pytest.raises(ObservationError):
        kubernetes.KubernetesClient().list_events('default', pod_name=name)
    cluster.CoreV1Api.return_value.list_namespaced_event.assert_not_called()


@pytest.mark.parametrize('tail', [1, 1000])
def test_log_tail_boundaries(cluster, tail):
    api = cluster.CoreV1Api.return_value
    api.read_namespaced_pod_log.return_value = 'ok'
    assert kubernetes.KubernetesClient().pod_logs('web', tail=tail) == 'ok'
    api.read_namespaced_pod_log.assert_called_once_with('web', 'default', tail_lines=tail, follow=False, _request_timeout=REQUEST_TIMEOUT)


@pytest.mark.parametrize('extra', [0, 1])
def test_log_bytes_and_cleanup(cluster, extra):
    from opspilot.limits import MAX_LOG_BYTES, ObservationLimitError
    api = cluster.CoreV1Api.return_value
    api.read_namespaced_pod_log.return_value = 'é' * (MAX_LOG_BYTES // 2) + 'a' * extra
    if extra:
        with pytest.raises(ObservationLimitError):
            kubernetes.KubernetesClient().pod_logs('web')
    else:
        assert len(kubernetes.KubernetesClient().pod_logs('web')) == MAX_LOG_BYTES // 2
    cluster.ApiClient.return_value.close.assert_called_once()
    cluster.ApiClient.return_value.rest_client.pool_manager.clear.assert_called_once()


@pytest.mark.parametrize('method,args', [('health_check', ()), ('inspect_pod', ('web',)), ('pod_logs', ('web',))])
def test_api_resources_closed_on_error(cluster, method, args):
    api = cluster.CoreV1Api.return_value
    mapping = {'health_check': 'get_api_resources', 'inspect_pod': 'read_namespaced_pod', 'pod_logs': 'read_namespaced_pod_log'}
    getattr(api, mapping[method]).side_effect = ApiException(status=500, reason='private')
    with pytest.raises(ApiException):
        getattr(kubernetes.KubernetesClient(), method)(*args)
    assert getattr(api, mapping[method]).call_args.kwargs['_request_timeout'] == REQUEST_TIMEOUT
    cluster.ApiClient.return_value.close.assert_called_once()
    cluster.ApiClient.return_value.rest_client.pool_manager.clear.assert_called_once()


def test_core_construction_error_closes_api_client(cluster):
    cluster.CoreV1Api.side_effect = RuntimeError('private')
    with pytest.raises(RuntimeError):
        kubernetes.KubernetesClient()
    cluster.ApiClient.return_value.close.assert_called_once()


def test_kubernetes_native_timeout_safe_and_closes(cluster):
    from urllib3.exceptions import ReadTimeoutError
    from opspilot.limits import ObservationTimeoutError
    cluster.CoreV1Api.return_value.get_api_resources.side_effect = ReadTimeoutError(None, 'private', 'private')
    with pytest.raises(ObservationTimeoutError, match='^Infrastructure observation timed out$'):
        kubernetes.KubernetesClient().health_check()
    cluster.ApiClient.return_value.close.assert_called_once()


def test_event_value_limit_after_sdk_projection(cluster):
    from opspilot.limits import MAX_VALUE_BYTES, ObservationLimitError
    event = make_event()
    event.message = 'é' * (MAX_VALUE_BYTES // 2 + 1)
    cluster.CoreV1Api.return_value.list_namespaced_event.return_value = paged([event])
    with pytest.raises(ObservationLimitError):
        kubernetes.KubernetesClient().list_events('default')
    cluster.ApiClient.return_value.close.assert_called_once()


def test_pagination_with_generated_kubernetes_models(cluster):
    from kubernetes.client import V1ListMeta, V1Namespace, V1NamespaceList, V1ObjectMeta
    api = cluster.CoreV1Api.return_value
    api.list_namespace.side_effect = [
        V1NamespaceList(items=[V1Namespace(metadata=V1ObjectMeta(name="zeta"))],
                        metadata=V1ListMeta(_continue="next-page")),
        V1NamespaceList(items=[V1Namespace(metadata=V1ObjectMeta(name="alpha"))],
                        metadata=V1ListMeta()),
    ]
    assert kubernetes.KubernetesClient().list_namespaces() == ["alpha", "zeta"]
    assert api.list_namespace.call_args_list == [
        call(limit=100, _request_timeout=REQUEST_TIMEOUT),
        call(limit=100, _request_timeout=REQUEST_TIMEOUT, _continue="next-page"),
    ]
    cluster.ApiClient.return_value.close.assert_called_once()


def test_credential_registration_survives_cleanup_and_second_observation(cluster):
    from opspilot.redaction import redactor, registry
    effective = cluster.CoreV1Api.return_value.api_client.configuration
    effective.api_key = {"authorization": "Bearer synthetic-first-observation-secret"}
    refreshes = []

    def refresh(current):
        refreshes.append(len(refreshes) + 1)
        current.api_key["authorization"] = f"Bearer synthetic-refresh-secret-{refreshes[-1]}"

    effective.refresh_api_key_hook = refresh
    instance = kubernetes.KubernetesClient()

    def discovery(**kwargs):
        assert kwargs == {"_request_timeout": REQUEST_TIMEOUT}
        effective.refresh_api_key_hook(effective)
        credential = effective.api_key["authorization"]
        assert credential in registry.values()
        assert credential.removeprefix("Bearer ") in registry.values()
        assert redactor.redact_text(credential.removeprefix("Bearer ")) == "[REDACTED]"

    cluster.CoreV1Api.return_value.get_api_resources.side_effect = discovery
    assert instance.health_check() is True
    cluster.ApiClient.return_value.rest_client.pool_manager.clear.assert_called_once()
    assert instance.health_check() is True
    assert refreshes == [1, 2]
    assert redactor.redact_text("synthetic-refresh-secret-1 synthetic-refresh-secret-2") == "[REDACTED] [REDACTED]"
    assert cluster.ApiClient.return_value.close.call_count == 2
    assert cluster.ApiClient.return_value.rest_client.pool_manager.clear.call_count == 2


def test_list_incremental_budget_matches_whole_result(cluster, monkeypatch):
    from opspilot.limits import ObservationBudget
    budget = ObservationBudget()
    monkeypatch.setattr(kubernetes, "ObservationBudget", lambda: budget)
    cluster.CoreV1Api.return_value.list_namespace.return_value = paged([
        SimpleNamespace(metadata=SimpleNamespace(name="alpha")),
        SimpleNamespace(metadata=SimpleNamespace(name="beta")),
    ])
    result = kubernetes.KubernetesClient().list_namespaces()
    whole = ObservationBudget()
    whole.add(result)
    assert budget.used == whole.used
