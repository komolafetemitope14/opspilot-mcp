from opspilot.diagnostics.correlation import CorrelationDiagnostics


def make_diagnosis(
    system: str,
    status: str,
) -> dict:
    return {
        "status": status,
        "system": system,
        "resource": {
            "kind": "TestResource",
        },
        "findings": [],
    }


def test_unhealthy_kubernetes_and_postgres_produce_correlation():
    diagnostics = CorrelationDiagnostics()

    kubernetes = make_diagnosis(
        system="kubernetes",
        status="unhealthy",
    )
    postgres = make_diagnosis(
        system="postgresql",
        status="unhealthy",
    )

    result = diagnostics.correlate(
        kubernetes=kubernetes,
        postgres=postgres,
    )

    finding = next(
        finding
        for finding in result["findings"]
        if finding["code"] == "possible_database_dependency_failure"
    )

    assert result["status"] == "unhealthy"
    assert result["system"] == "cross-system"
    assert result["resource"] == {
        "kind": "Correlation",
    }
    assert finding["severity"] == "error"


def test_unhealthy_kubernetes_and_healthy_postgres_have_no_correlation():
    diagnostics = CorrelationDiagnostics()

    result = diagnostics.correlate(
        kubernetes=make_diagnosis("kubernetes", "unhealthy"),
        postgres=make_diagnosis("postgresql", "healthy"),
    )

    assert result == {
        "status": "healthy",
        "system": "cross-system",
        "resource": {
            "kind": "Correlation",
        },
        "findings": [],
    }


def test_healthy_kubernetes_and_unhealthy_postgres_have_no_correlation():
    diagnostics = CorrelationDiagnostics()

    result = diagnostics.correlate(
        kubernetes=make_diagnosis("kubernetes", "healthy"),
        postgres=make_diagnosis("postgresql", "unhealthy"),
    )

    assert result == {
        "status": "healthy",
        "system": "cross-system",
        "resource": {
            "kind": "Correlation",
        },
        "findings": [],
    }


def test_healthy_kubernetes_and_postgres_have_no_correlation():
    diagnostics = CorrelationDiagnostics()

    result = diagnostics.correlate(
        kubernetes=make_diagnosis("kubernetes", "healthy"),
        postgres=make_diagnosis("postgresql", "healthy"),
    )

    assert result == {
        "status": "healthy",
        "system": "cross-system",
        "resource": {
            "kind": "Correlation",
        },
        "findings": [],
    }
