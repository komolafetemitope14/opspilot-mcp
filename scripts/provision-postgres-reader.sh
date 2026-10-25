#!/bin/sh
# Run with the project's Python dependencies installed (activate .venv first).
# Exported configuration takes precedence over the repository's ignored .env.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 - "$script_dir/../.env" <<'PY'
import os
import sys

import psycopg
from psycopg import sql
from dotenv import load_dotenv


def main():
    load_dotenv(sys.argv[1], override=False)
    required = (
        "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
        "POSTGRES_ADMIN_USER", "POSTGRES_ADMIN_PASSWORD",
        "POSTGRES_USER", "POSTGRES_PASSWORD",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print("Missing required configuration: " + ", ".join(missing), file=sys.stderr)
        return 1
    if os.environ["POSTGRES_USER"] != "opspilot_reader":
        print("POSTGRES_USER must be opspilot_reader", file=sys.stderr)
        return 1
    if os.environ["POSTGRES_ADMIN_USER"] == "opspilot_reader":
        print("POSTGRES_ADMIN_USER must differ from opspilot_reader", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=os.environ["POSTGRES_PORT"],
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_ADMIN_USER"],
            password=os.environ["POSTGRES_ADMIN_PASSWORD"],
        ) as connection:
            with connection.cursor() as cursor:
                # Serialize concurrent runs; all changes commit together.
                cursor.execute("SELECT pg_advisory_xact_lock(1879048192, 6)")
                cursor.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s",
                    ("opspilot_reader",),
                )
                if cursor.fetchone() is None:
                    cursor.execute("CREATE ROLE opspilot_reader")
                cursor.execute(sql.SQL(
                    "ALTER ROLE opspilot_reader WITH LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Literal(os.environ["POSTGRES_PASSWORD"])))
                cursor.execute(sql.SQL(
                    "GRANT CONNECT ON DATABASE {} TO opspilot_reader"
                ).format(sql.Identifier(os.environ["POSTGRES_DB"])))
                cursor.execute("GRANT USAGE ON SCHEMA public TO opspilot_reader")
                cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO opspilot_reader")
                cursor.execute(sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT ON TABLES TO opspilot_reader"
                ).format(sql.Identifier(os.environ["POSTGRES_ADMIN_USER"])))
    except Exception:
        # Driver errors can contain SQL and credentials. Never echo them.
        print("Reader provisioning failed; transaction rolled back. Check admin "
              "credentials, connectivity, and privileges.", file=sys.stderr)
        return 1
    print("Provisioned opspilot_reader and SELECT grants.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY
