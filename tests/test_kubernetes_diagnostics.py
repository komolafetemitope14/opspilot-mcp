from opspilot.diagnostics.kubernetes import KubernetesDiagnostics


def make_pod(
    *,
    name="opspilot-demo",
    namespace="default",
    phase="Running",
    ready=True,
    restarts=0,
):
    return {
        "name": name,
        "namespace": namespace,
        "phase": phase,
        "ready": ready,
        "restarts": restarts,
        "node": "opspilot-control-plane",
        "pod_ip": "10.244.0.5",
    }


def make_event(
    *,
    name="opspilot-demo",
    namespace="default",
    event_type="Normal",
    reason="Started",
    message="Started container",
):
    return {
        "namespace": namespace,
        "kind": "Pod",
        "name": name,
        "type": event_type,
        "reason": reason,
        "message": message,
        "count": 1,
        "first_timestamp": None,
        "last_timestamp": None,
    }


def test_healthy_pod_has_no_findings():
    diagnostics = KubernetesDiagnostics()

    result = diagnostics.diagnose_pod(make_pod(), [])

    assert result == {
        "status": "healthy",
        "system": "kubernetes",
        "resource": {
            "kind": "Pod",
            "name": "opspilot-demo",
            "namespace": "default",
        },
        "findings": [],
    }


def test_not_ready_pod_produces_warning():
    diagnostics = KubernetesDiagnostics()

    result = diagnostics.diagnose_pod(make_pod(ready=False), [])

    assert result["status"] == "degraded"
    assert result["findings"][0]["code"] == "pod_not_ready"
    assert result["findings"][0]["severity"] == "warning"


def test_non_running_pod_produces_warning():
    diagnostics = KubernetesDiagnostics()

    result = diagnostics.diagnose_pod(make_pod(phase="Pending"), [])

    assert result["status"] == "degraded"
    assert result["findings"][0]["code"] == "pod_not_running"
    assert result["findings"][0]["severity"] == "warning"


def test_restarts_produce_warning():
    diagnostics = KubernetesDiagnostics()

    result = diagnostics.diagnose_pod(make_pod(restarts=3), [])

    assert result["status"] == "degraded"
    assert result["findings"][0]["code"] == "container_restarts"
    assert result["findings"][0]["severity"] == "warning"
    assert "3" in result["findings"][0]["evidence"][0]


def test_image_pull_backoff_produces_error():
    diagnostics = KubernetesDiagnostics()
    events = [
        make_event(
            event_type="Warning",
            reason="Failed",
            message="Error: ImagePullBackOff",
        )
    ]

    result = diagnostics.diagnose_pod(make_pod(ready=False), events)

    assert result["status"] == "unhealthy"

    finding = next(
        finding
        for finding in result["findings"]
        if finding["code"] == "image_pull_failure"
    )

    assert finding["severity"] == "error"
    assert "ImagePullBackOff" in finding["evidence"][0]


def test_err_image_pull_produces_error():
    diagnostics = KubernetesDiagnostics()
    events = [
        make_event(
            event_type="Warning",
            reason="Failed",
            message="Error: ErrImagePull",
        )
    ]

    result = diagnostics.diagnose_pod(make_pod(ready=False), events)

    assert result["status"] == "unhealthy"
    assert any(
        finding["code"] == "image_pull_failure"
        for finding in result["findings"]
    )


def test_crash_loop_backoff_produces_error():
    diagnostics = KubernetesDiagnostics()
    events = [
        make_event(
            event_type="Warning",
            reason="BackOff",
            message="Back-off restarting failed container: CrashLoopBackOff",
        )
    ]

    result = diagnostics.diagnose_pod(make_pod(restarts=5), events)

    assert result["status"] == "unhealthy"
    assert any(
        finding["code"] == "container_crash_loop"
        for finding in result["findings"]
    )


def test_warning_event_produces_warning_finding():
    diagnostics = KubernetesDiagnostics()
    events = [
        make_event(
            event_type="Warning",
            reason="FailedScheduling",
            message="0/1 nodes are available",
        )
    ]

    result = diagnostics.diagnose_pod(make_pod(), events)

    assert result["status"] == "degraded"
    assert any(
        finding["code"] == "kubernetes_warning_event"
        for finding in result["findings"]
    )


def test_multiple_image_pull_events_produce_one_finding():
    diagnostics = KubernetesDiagnostics()
    events = [
        make_event(
            event_type="Warning",
            reason="Failed",
            message="Error: ErrImagePull",
        ),
        make_event(
            event_type="Warning",
            reason="Failed",
            message="Error: ImagePullBackOff",
        ),
    ]

    result = diagnostics.diagnose_pod(
        make_pod(phase="Pending", ready=False),
        events,
    )

    image_pull_findings = [
        finding
        for finding in result["findings"]
        if finding["code"] == "image_pull_failure"
    ]

    assert len(image_pull_findings) == 1
    assert image_pull_findings[0]["evidence"] == [
        "Error: ErrImagePull",
        "Error: ImagePullBackOff",
    ]


def test_findings_exact_cap_and_overflow():
    import pytest
    from opspilot.limits import ObservationLimitError
    diagnostics = KubernetesDiagnostics()
    events = [make_event(event_type='Warning', message='warning')] * 100
    assert len(diagnostics.diagnose_pod(make_pod(), events)['findings']) == 100
    with pytest.raises(ObservationLimitError):
        diagnostics.diagnose_pod(make_pod(ready=False), events)


def test_grouped_evidence_cannot_bypass_bound():
    import pytest
    from opspilot.limits import ObservationLimitError
    events = [make_event(message=f'ImagePullBackOff {n}') for n in range(101)]
    with pytest.raises(ObservationLimitError):
        KubernetesDiagnostics().diagnose_pod(make_pod(), events)
