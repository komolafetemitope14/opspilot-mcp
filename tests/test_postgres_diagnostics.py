from opspilot.diagnostics.postgres import PostgresDiagnostics


def test_healthy_database_with_tables():
    diagnostics = PostgresDiagnostics()

    result = diagnostics.diagnose_database(
        healthy=True,
        tables=["services"],
    )

    assert result == {
        "status": "healthy",
        "system": "postgresql",
        "resource": {
            "kind": "Database",
        },
        "findings": [],
    }


def test_failed_health_check_produces_error():
    diagnostics = PostgresDiagnostics()

    result = diagnostics.diagnose_database(
        healthy=False,
        tables=["services"],
    )

    finding = next(
        finding
        for finding in result["findings"]
        if finding["code"] == "database_health_check_failed"
    )

    assert result["status"] == "unhealthy"
    assert finding["severity"] == "error"


def test_no_public_tables_produces_warning():
    diagnostics = PostgresDiagnostics()

    result = diagnostics.diagnose_database(
        healthy=True,
        tables=[],
    )

    finding = next(
        finding
        for finding in result["findings"]
        if finding["code"] == "no_public_tables"
    )

    assert result["status"] == "degraded"
    assert finding["severity"] == "warning"


def test_failed_health_check_has_priority_over_warning():
    diagnostics = PostgresDiagnostics()

    result = diagnostics.diagnose_database(
        healthy=False,
        tables=[],
    )

    codes = {
        finding["code"]
        for finding in result["findings"]
    }

    assert result["status"] == "unhealthy"
    assert codes == {
        "database_health_check_failed",
        "no_public_tables",
    }
