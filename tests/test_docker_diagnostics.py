from opspilot.diagnostics.docker import DockerDiagnostics


def make_container(
    *,
    name="example-container",
    status="running",
    health="healthy",
    running=True,
    exit_code=0,
):
    return {
        "id": "abc123",
        "name": name,
        "image": "example:latest",
        "status": status,
        "health": health,
        "running": running,
        "exit_code": exit_code,
        "ports": {},
    }


def test_healthy_container():
    diagnostics = DockerDiagnostics()

    result = diagnostics.diagnose_container(
        make_container()
    )

    assert result == {
        "status": "healthy",
        "system": "docker",
        "resource": {
            "kind": "Container",
            "name": "example-container",
        },
        "findings": [],
    }


def test_container_not_running():
    diagnostics = DockerDiagnostics()

    result = diagnostics.diagnose_container(
        make_container(
            status="exited",
            health=None,
            running=False,
        )
    )

    finding = next(
        finding
        for finding in result["findings"]
        if finding["code"] == "container_not_running"
    )

    assert result["status"] == "degraded"
    assert finding["severity"] == "warning"
    assert "exited" in finding["evidence"][0]


def test_container_nonzero_exit():
    diagnostics = DockerDiagnostics()

    result = diagnostics.diagnose_container(
        make_container(
            status="exited",
            health=None,
            running=False,
            exit_code=1,
        )
    )

    finding = next(
        finding
        for finding in result["findings"]
        if finding["code"] == "container_nonzero_exit"
    )

    assert result["status"] == "unhealthy"
    assert finding["severity"] == "error"
    assert "1" in finding["evidence"][0]


def test_container_unhealthy():
    diagnostics = DockerDiagnostics()

    result = diagnostics.diagnose_container(
        make_container(
            health="unhealthy",
        )
    )

    finding = next(
        finding
        for finding in result["findings"]
        if finding["code"] == "container_unhealthy"
    )

    assert result["status"] == "unhealthy"
    assert finding["severity"] == "error"


def test_container_health_starting():
    diagnostics = DockerDiagnostics()

    result = diagnostics.diagnose_container(
        make_container(
            health="starting",
        )
    )

    finding = next(
        finding
        for finding in result["findings"]
        if finding["code"] == "container_health_starting"
    )

    assert result["status"] == "degraded"
    assert finding["severity"] == "warning"


def test_multiple_findings_use_highest_severity():
    diagnostics = DockerDiagnostics()

    result = diagnostics.diagnose_container(
        make_container(
            status="exited",
            health="unhealthy",
            running=False,
            exit_code=2,
        )
    )

    codes = {
        finding["code"]
        for finding in result["findings"]
    }

    assert result["status"] == "unhealthy"
    assert codes == {
        "container_not_running",
        "container_nonzero_exit",
        "container_unhealthy",
    }
