"""Small synchronous PostgreSQL client configured through the environment."""

from typing import Any

import psycopg
import sqlparse
from psycopg.rows import dict_row


from opspilot.configuration import PostgresConfig, load_postgres_config


class PostgresClient:
    def __init__(self, configuration: PostgresConfig | None = None) -> None:
        settings = configuration if configuration is not None else load_postgres_config()
        self._connection_kwargs = settings.connection_kwargs()

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
            "ORDER BY table_name"
        )
        return [row["table_name"] for row in rows]

    def execute_select(self, query: str) -> list[dict[str, Any]]:
        """Execute one SELECT in a read-only transaction and return named rows."""
        statements = sqlparse.parse(query)
        if len(statements) != 1 or statements[0].get_type() != "SELECT":
            raise ValueError("Expected a single SELECT statement")

        with psycopg.connect(**self._connection_kwargs) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(query)
                return cursor.fetchall()
