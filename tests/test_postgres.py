from unittest.mock import MagicMock

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.rows import dict_row

from opspilot.clients import postgres


@pytest.fixture
def config(monkeypatch):
    values = {
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "test_db",
        "POSTGRES_USER": "test_user",
        "POSTGRES_PASSWORD": "test_password",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(postgres, "load_dotenv", MagicMock())
    return values


@pytest.fixture
def database(monkeypatch, config):
    connect = MagicMock()
    monkeypatch.setattr(postgres.psycopg, "connect", connect)
    connection = connect.return_value.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    return connect, connection, cursor


def test_configuration_comes_from_environment(config, database):
    connect, _, cursor = database
    cursor.fetchone.return_value = (1,)
    assert postgres.PostgresClient().health_check() is True
    connect.assert_called_once_with(
        host="localhost", port="5432", dbname="test_db",
        user="test_user", password="test_password",
    )
    postgres.load_dotenv.assert_called_once_with(override=False)


def test_dotenv_loading_preserves_environment(monkeypatch, tmp_path, config):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_HOST=file-host\nPOSTGRES_DB=file-db\n")
    monkeypatch.delenv("POSTGRES_DB")
    monkeypatch.setattr(
        postgres, "load_dotenv",
        lambda **kwargs: load_dotenv(env_file, **kwargs),
    )
    client = postgres.PostgresClient()
    assert client._connection_kwargs["host"] == "localhost"
    assert client._connection_kwargs["dbname"] == "file-db"


@pytest.mark.parametrize("value", [None, ""])
def test_missing_configuration(monkeypatch, config, value):
    if value is None:
        monkeypatch.delenv("POSTGRES_PASSWORD")
    else:
        monkeypatch.setenv("POSTGRES_PASSWORD", value)
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD"):
        postgres.PostgresClient()


def test_health_check_executes_select_one(database):
    _, _, cursor = database
    cursor.fetchone.return_value = (1,)
    assert postgres.PostgresClient().health_check() is True
    cursor.execute.assert_called_once_with("SELECT 1")


@pytest.mark.parametrize("rows", [[], [{"answer": 42}]])
@pytest.mark.parametrize("query", ["SELECT 42", "-- comment\nselect 42;", "SELECT ';'"])
def test_select_uses_read_only_transaction(database, query, rows):
    connect, connection, cursor = database
    cursor.fetchall.return_value = rows
    assert postgres.PostgresClient().execute_select(query) == rows
    connection.cursor.assert_called_once_with(row_factory=dict_row)
    assert [call.args[0] for call in cursor.execute.call_args_list] == [
        "SET TRANSACTION READ ONLY", query,
    ]
    connection.cursor.return_value.__exit__.assert_called_once()
    connect.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("query", [
    "", "-- comment only", "INSERT INTO items VALUES (1)",
    "UPDATE items SET id = 2", "DELETE FROM items", "DROP TABLE items",
    "SHOW tables", "SELECT 1; SELECT 2", "SELECT 1; DELETE FROM items",
])
def test_rejects_invalid_queries_before_connecting(database, query):
    connect, _, _ = database
    with pytest.raises(ValueError, match="single SELECT"):
        postgres.PostgresClient().execute_select(query)
    connect.assert_not_called()


def test_list_tables(database):
    _, _, cursor = database
    cursor.fetchall.return_value = [{"table_name": "alpha"}, {"table_name": "beta"}]
    assert postgres.PostgresClient().list_tables() == ["alpha", "beta"]
    query = cursor.execute.call_args.args[0]
    assert "table_schema = 'public'" in query
    assert "table_type = 'BASE TABLE'" in query
    assert "ORDER BY table_name" in query


def test_query_error_propagates_and_contexts_exit(database):
    connect, connection, cursor = database
    cursor.execute.side_effect = [None, psycopg.errors.ReadOnlySqlTransaction("read only")]
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        postgres.PostgresClient().execute_select("SELECT 1")
    connection.cursor.return_value.__exit__.assert_called_once()
    connect.return_value.__exit__.assert_called_once()


def test_connection_error_propagates(database):
    connect, _, _ = database
    connect.side_effect = psycopg.OperationalError("unavailable")
    with pytest.raises(psycopg.OperationalError):
        postgres.PostgresClient().health_check()
