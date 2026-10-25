"""Provisioning checks without connecting to PostgreSQL or invoking Docker."""

from pathlib import Path
import subprocess
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/provision-postgres-reader.sh"


@pytest.fixture
def provision(monkeypatch):
    # Exercise the exact Python payload executed by the shell entry point.
    source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    namespace = {"__name__": "provision_test"}
    exec(compile(source, str(SCRIPT), "exec"), namespace)
    values = {
        "POSTGRES_HOST": "localhost", "POSTGRES_PORT": "5432",
        "POSTGRES_DB": 'db"name', "POSTGRES_ADMIN_USER": 'owner"name',
        "POSTGRES_ADMIN_PASSWORD": "admin-secret",
        "POSTGRES_USER": "opspilot_reader", "POSTGRES_PASSWORD": "reader's-secret",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    dotenv = MagicMock()
    monkeypatch.setitem(namespace, "load_dotenv", dotenv)
    monkeypatch.setattr(namespace["sys"], "argv", ["-", "/ignored/.env"])
    connect = MagicMock()
    monkeypatch.setattr(namespace["psycopg"], "connect", connect)
    connection = connect.return_value.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    return namespace["main"], connect, connection, cursor, dotenv


@pytest.mark.parametrize("existing", [False, True])
def test_provisions_new_and_existing_role(provision, existing, capsys):
    main, connect, connection, cursor, dotenv = provision
    cursor.fetchone.return_value = (1,) if existing else None
    assert main() == 0
    dotenv.assert_called_once_with("/ignored/.env", override=False)
    connect.assert_called_once_with(
        host="localhost", port="5432", dbname='db"name',
        user='owner"name', password="admin-secret",
    )
    statements = [
        call.args[0] if isinstance(call.args[0], str) else call.args[0].as_string()
        for call in cursor.execute.call_args_list
    ]
    assert ("CREATE ROLE opspilot_reader" in statements) is not existing
    assert (
        "ALTER ROLE opspilot_reader WITH LOGIN NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'reader''s-secret'"
    ) in statements
    assert 'GRANT CONNECT ON DATABASE "db""name" TO opspilot_reader' in statements
    assert "GRANT USAGE ON SCHEMA public TO opspilot_reader" in statements
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO opspilot_reader" in statements
    assert (
        'ALTER DEFAULT PRIVILEGES FOR ROLE "owner""name" IN SCHEMA public '
        'GRANT SELECT ON TABLES TO opspilot_reader'
    ) in statements
    assert not any("OWNER TO" in statement for statement in statements)
    connection.__exit__.assert_not_called()  # Connection context belongs to connect().
    connect.return_value.__exit__.assert_called_once_with(None, None, None)
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("name", [
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_ADMIN_USER",
    "POSTGRES_ADMIN_PASSWORD", "POSTGRES_USER", "POSTGRES_PASSWORD",
])
@pytest.mark.parametrize("value", [None, ""])
def test_missing_configuration_fails_before_connection(provision, monkeypatch, capsys, name, value):
    main, connect, *_ = provision
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    assert main() == 1
    assert name in capsys.readouterr().err
    connect.assert_not_called()


@pytest.mark.parametrize("name,value", [
    ("POSTGRES_USER", "other_reader"),
    ("POSTGRES_ADMIN_USER", "opspilot_reader"),
])
def test_rejects_incorrect_roles(provision, monkeypatch, capsys, name, value):
    main, connect, *_ = provision
    monkeypatch.setenv(name, value)
    assert main() == 1
    assert name in capsys.readouterr().err
    connect.assert_not_called()


def test_sql_failure_rolls_back_without_exposing_error(provision, capsys):
    main, connect, _, cursor, _ = provision
    cursor.execute.side_effect = RuntimeError("reader's-secret admin-secret")
    assert main() == 1
    assert connect.return_value.__exit__.call_args.args[0] is RuntimeError
    output = capsys.readouterr()
    assert "provisioning failed" in output.err
    assert "secret" not in output.out + output.err


def test_compose_uses_only_admin_credentials_and_preserves_volume():
    compose = (ROOT / "compose.yaml").read_text()
    assert "POSTGRES_USER: ${POSTGRES_ADMIN_USER:?" in compose
    assert "POSTGRES_PASSWORD: ${POSTGRES_ADMIN_PASSWORD:?" in compose
    assert "${POSTGRES_USER" not in compose
    assert "${POSTGRES_PASSWORD" not in compose
    assert 'pg_isready -U \\"$$POSTGRES_USER\\" -d \\"$$POSTGRES_DB\\"' in compose
    assert "postgres_data:/var/lib/postgresql/data" in compose
    assert compose.endswith("volumes:\n  postgres_data:\n")


def test_example_has_separate_credentials_without_passwords():
    values = dict(line.split("=", 1) for line in (ROOT / ".env.example").read_text().splitlines())
    assert values["POSTGRES_USER"] == "opspilot_reader"
    assert values["POSTGRES_ADMIN_USER"] != "opspilot_reader"
    assert values["POSTGRES_PASSWORD"] == values["POSTGRES_ADMIN_PASSWORD"] == ""


def test_shell_entry_point_syntax():
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True, capture_output=True)
