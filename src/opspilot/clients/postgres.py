"""Small synchronous PostgreSQL client configured through the environment."""

import os
from typing import Any

import psycopg
import sqlparse
from dotenv import load_dotenv
from psycopg.rows import dict_row


from opspilot.redaction import registry


class PostgresClient:
    def __init__(self) -> None:
        load_dotenv(override=False)
        registry.register_environment(os.environ)
        names = {
            "host": "POSTGRES_HOST",
            "port": "POSTGRES_PORT",
            "dbname": "POSTGRES_DB",
            "user": "POSTGRES_USER",
            "password": "POSTGRES_PASSWORD",
        }
        missing = [name for name in names.values() if not os.getenv(name)]
        if missing:
            raise ValueError(f"Missing database configuration: {', '.join(missing)}")
        self._connection_kwargs = {key: os.environ[name] for key, name in names.items()}

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
