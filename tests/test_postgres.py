from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.clients import postgres
from opspilot import configuration


def iter_stream(rows):
    yield from rows


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
    monkeypatch.setattr(configuration, "find_dotenv", lambda: "")
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
        connect_timeout=5, options="-c statement_timeout=10000",
    )


def test_dotenv_loading_preserves_environment(monkeypatch, tmp_path, config):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_HOST=file-host\nPOSTGRES_DB=file-db\n")
    monkeypatch.delenv("POSTGRES_DB")
    monkeypatch.setattr(configuration, "find_dotenv", lambda: str(env_file))
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
    cursor.stream.return_value = iter_stream(rows)
    assert postgres.PostgresClient().execute_select(query) == rows
    connection.cursor.assert_called_once_with(row_factory=dict_row)
    assert [call.args[0] for call in cursor.execute.call_args_list] == [
        "SET TRANSACTION READ ONLY",
    ]
    cursor.stream.assert_called_once_with(query)
    cursor.fetchall.assert_not_called()
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
    cursor.stream.return_value = iter_stream([{ "table_name": "alpha"}, {"table_name": "beta"}])
    assert postgres.PostgresClient().list_tables() == ["alpha", "beta"]
    query = cursor.stream.call_args.args[0]
    assert "table_schema = 'public'" in query
    assert "table_type = 'BASE TABLE'" in query
    assert "ORDER BY table_name" in query


def test_query_error_propagates_and_contexts_exit(database):
    connect, connection, cursor = database
    cursor.stream.side_effect = psycopg.errors.ReadOnlySqlTransaction("read only")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        postgres.PostgresClient().execute_select("SELECT 1")
    connection.cursor.return_value.__exit__.assert_called_once()
    connect.return_value.__exit__.assert_called_once()


def test_connection_error_propagates(database):
    connect, _, _ = database
    connect.side_effect = psycopg.OperationalError("unavailable")
    with pytest.raises(psycopg.OperationalError):
        postgres.PostgresClient().health_check()


def test_effective_password_registered_before_connection_failure(config, database):
    from opspilot.redaction import redactor
    connect, _, _ = database
    def fail(**kwargs):
        assert redactor.redact_text(kwargs["password"]) == "[REDACTED]"
        raise psycopg.OperationalError("connection failed")
    connect.side_effect = fail
    with pytest.raises(psycopg.OperationalError):
        postgres.PostgresClient().health_check()


def test_configuration_failure_prevents_connection_activation(monkeypatch):
    error = configuration.ConfigurationError("POSTGRES_PORT is invalid")
    loader = MagicMock(side_effect=error)
    connect = MagicMock(side_effect=AssertionError("Must not connect"))
    monkeypatch.setattr(postgres, "load_postgres_config", loader)
    monkeypatch.setattr(postgres.psycopg, "connect", connect)
    with pytest.raises(configuration.ConfigurationError) as caught:
        postgres.PostgresClient().health_check()
    assert caught.value is error
    loader.assert_called_once_with()
    connect.assert_not_called()


@pytest.mark.parametrize('count', [500, 501])
def test_stream_row_limit(database, count):
    from opspilot.limits import ObservationLimitError
    _, connection, cursor = database
    cursor.stream.return_value = iter_stream({'n': n} for n in range(count))
    if count == 500:
        assert len(postgres.PostgresClient().execute_select('SELECT n')) == 500
        connection.close.assert_not_called()
    else:
        with pytest.raises(ObservationLimitError):
            postgres.PostgresClient().execute_select('SELECT n')
        connection.close.assert_called_once()
    cursor.fetchall.assert_not_called()


@pytest.mark.parametrize('rows', [ [{'v': 'é' * 32769}], [{'v': 'x' * 65536}] * 17 ])
def test_stream_byte_limits(database, rows):
    from opspilot.limits import ObservationLimitError
    _, connection, cursor = database
    cursor.stream.return_value = iter_stream(rows)
    with pytest.raises(ObservationLimitError):
        postgres.PostgresClient().execute_select('SELECT v')
    connection.close.assert_called_once()


def test_overflow_closes_before_stream_finalizer_and_never_drains(database):
    from opspilot.limits import ObservationLimitError
    _, connection, cursor = database
    events = []
    connection.close.side_effect = lambda: events.append('closed')
    def endless():
        try:
            for n in range(502):
                if n == 501:
                    pytest.fail('overflow drained another row')
                yield {'n': n}
        finally:
            assert events == ['closed']
            events.append('finalized')
    cursor.stream.return_value = endless()
    with pytest.raises(ObservationLimitError):
        postgres.PostgresClient().execute_select('SELECT n')
    assert events == ['closed', 'finalized']


def test_installed_psycopg_stream_finalizer_skips_drain_after_close():
    # Exercise the installed generator, not a copy of its cleanup logic.
    from contextlib import nullcontext
    from types import SimpleNamespace
    from psycopg import Cursor
    from psycopg.pq import TransactionStatus
    fake = MagicMock()
    fake._pgconn = SimpleNamespace(pipeline_status=0, transaction_status=TransactionStatus.ACTIVE)
    fake._conn.lock = nullcontext()
    fake._conn.wait.side_effect = [None, SimpleNamespace(ntuples=1)]
    fake._tx.load_row.return_value = {'n': 1}
    stream = Cursor.stream(fake, 'SELECT n')
    assert next(stream) == {'n': 1}
    # PQfinish changes transaction status to UNKNOWN on a closed connection.
    fake._pgconn.transaction_status = TransactionStatus.UNKNOWN
    stream.close()
    fake._conn._try_cancel.assert_not_called()
    assert fake._conn.wait.call_count == 2


def test_owned_table_query_has_cap_plus_one(database):
    _, _, cursor = database
    cursor.stream.return_value = iter_stream([])
    postgres.PostgresClient().list_tables()
    assert cursor.stream.call_args.args[0].endswith('LIMIT 501')


@pytest.mark.parametrize('error', [psycopg.errors.QueryCanceled, psycopg.errors.ConnectionTimeout])
def test_native_timeout_is_safe(database, error):
    from opspilot.limits import ObservationTimeoutError
    connect, _, _ = database
    connect.side_effect = error('credential=must-not-publish')
    with pytest.raises(ObservationTimeoutError, match='^Infrastructure observation timed out$'):
        postgres.PostgresClient().health_check()


def test_composite_field_cannot_bypass_value_limit(database):
    from opspilot.limits import ObservationLimitError
    _, connection, cursor = database
    cursor.stream.return_value = iter_stream([{'v': ['x' * 1024] * 65}])
    with pytest.raises(ObservationLimitError):
        postgres.PostgresClient().execute_select('SELECT v')
    connection.close.assert_called_once()


def test_value_exact_boundary_succeeds(database):
    _, _, cursor = database
    rows = [{'v': 'é' * 32768}]
    cursor.stream.return_value = iter_stream(rows)
    assert postgres.PostgresClient().execute_select('SELECT v') == rows


@pytest.mark.parametrize("count", [500, 501])
def test_psycopg_31_stream_signature_unchanged_sql_and_row_policy(database, count):
    from opspilot.limits import ObservationLimitError
    _, connection, cursor = database
    query = "-- preserve SQL exactly\nSELECT n FROM numbers ORDER BY n;"
    received = []

    def stream(only_query):
        received.append(only_query)
        assert cursor.execute.call_args.args == ("SET TRANSACTION READ ONLY",)
        yield from ({"n": n} for n in range(count))

    cursor.stream = stream  # A real signature; unexpected kwargs raise TypeError.
    if count == 500:
        assert len(postgres.PostgresClient().execute_select(query)) == 500
    else:
        with pytest.raises(ObservationLimitError):
            postgres.PostgresClient().execute_select(query)
        connection.close.assert_called_once()
    assert received == [query]


def test_database_exception_during_stream_closes_resources(database):
    connect, connection, cursor = database
    error = psycopg.errors.DataException("synthetic-private-database-message")
    events = []

    def stream(only_query):
        try:
            yield {"n": 1}
            raise error
        finally:
            events.append("stream finalized")

    cursor.stream = stream
    connection.close.side_effect = lambda: events.append("connection closed")
    with pytest.raises(psycopg.errors.DataException) as caught:
        postgres.PostgresClient().execute_select("SELECT n")
    assert caught.value is error
    assert events == ["stream finalized", "connection closed"]
    connection.close.assert_called_once()
    connection.cursor.return_value.__exit__.assert_called_once()
    connect.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("count", [500, 501])
def test_list_tables_direct_item_boundaries(database, count):
    from opspilot.limits import ObservationLimitError
    _, connection, cursor = database
    rows = [{"table_name": f"table_{n:03d}"} for n in range(count)]
    cursor.stream.return_value = iter_stream(rows)
    if count == 500:
        assert postgres.PostgresClient().list_tables() == [row["table_name"] for row in rows]
    else:
        with pytest.raises(ObservationLimitError):
            postgres.PostgresClient().list_tables()
        connection.close.assert_called_once()
    assert cursor.stream.call_args.args[0].endswith("LIMIT 501")


def test_stream_incremental_budget_matches_whole_result(database, monkeypatch):
    from opspilot.limits import ObservationBudget
    budget = ObservationBudget()
    monkeypatch.setattr(postgres, "ObservationBudget", lambda: budget)
    _, _, cursor = database
    cursor.stream.return_value = iter_stream([{"v": "é"}, {"v": "🙂"}])
    result = postgres.PostgresClient().execute_select("SELECT v")
    whole = ObservationBudget()
    whole.add(result)
    assert budget.used == whole.used
