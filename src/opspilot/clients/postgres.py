"""Small synchronous PostgreSQL client configured through the environment."""

from typing import Any
from functools import wraps

import psycopg
import sqlparse
from psycopg.rows import dict_row


from opspilot.configuration import PostgresConfig, load_postgres_config
from opspilot.limits import (
    POSTGRES_CONNECT_TIMEOUT_SECONDS, POSTGRES_STATEMENT_TIMEOUT_MS,
    MAX_QUERY_ROWS, MAX_LIST_ITEMS, ObservationBudget, ObservationLimitError,
    ObservationTimeoutError, check_value,
)


def _safe_timeout(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (psycopg.errors.QueryCanceled, psycopg.errors.ConnectionTimeout):
            raise ObservationTimeoutError() from None
    return wrapped


class PostgresClient:
    def __init__(self, configuration: PostgresConfig | None = None) -> None:
        settings = configuration if configuration is not None else load_postgres_config()
        # Explicit startup options cover health/transaction setup too. This is
        # not a client network deadline; SQL functions can change session GUCs.
        self._connection_kwargs = {
            **settings.connection_kwargs(),
            "connect_timeout": POSTGRES_CONNECT_TIMEOUT_SECONDS,
            "options": f"-c statement_timeout={POSTGRES_STATEMENT_TIMEOUT_MS}",
        }

    @_safe_timeout
    def health_check(self) -> bool:
        """Return True when SELECT 1 succeeds; connection errors propagate."""
        with psycopg.connect(**self._connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)

    def list_tables(self) -> list[str]:
        """Return sorted public-schema base-table names."""
        rows = self.execute_select(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            f"ORDER BY table_name LIMIT {MAX_LIST_ITEMS + 1}"
        )
        return [row["table_name"] for row in rows]

    @_safe_timeout
    def execute_select(self, query: str) -> list[dict[str, Any]]:
        """Execute one SELECT in a read-only transaction and return named rows."""
        statements = sqlparse.parse(query)
        if len(statements) != 1 or statements[0].get_type() != "SELECT":
            raise ValueError("Expected a single SELECT statement")

        with psycopg.connect(**self._connection_kwargs) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                # Single-row mode avoids buffering the complete result. One row
                # (including a huge value) can still exist before our checks.
                rows = []
                budget = ObservationBudget().start_list()
                stream = cursor.stream(query)
                try:
                    for row in stream:
                        if len(rows) >= MAX_QUERY_ROWS:
                            raise ObservationLimitError()
                        for value in row.values():
                            check_value(value)
                        budget.add_item(row)
                        rows.append(row)
                except BaseException:
                    # Close BEFORE closing the generator: psycopg's stream
                    # finalizer otherwise cancels and drains the active query.
                    connection.close()
                    raise
                finally:
                    stream.close()
                return rows
