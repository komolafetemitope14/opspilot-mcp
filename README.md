# OpsPilot MCP

OpsPilot MCP is a Python-based local infrastructure MCP server in development that will allow AI assistants to inspect and diagnose PostgreSQL, Docker, and Kubernetes through the Model Context Protocol (MCP).

This README is a living project progress document, tracking implemented capabilities, verification results, and upcoming milestones.

## Project Goal

Enable AI assistants to answer infrastructure questions such as:

- Is PostgreSQL healthy?
- What tables exist?
- What Docker containers are running?
- Show container logs.
- What Kubernetes pods are running?
- Why is my application failing?

## Architecture

The planned architecture:

```text
AI / MCP Client
       |
       | MCP
       v
OpsPilot MCP Server
       |
       +---- PostgreSQL
       +---- Docker
       +---- Kubernetes
```

## Current Status

**Milestone 1 — PostgreSQL Client: COMPLETE**

Implemented and verified:

- Synchronous `PostgresClient` using Psycopg 3.
- `health_check()` using `SELECT 1`.
- `list_tables()` for public-schema base tables.
- `execute_select()` for a single SELECT query.
- Dictionary query results.
- Rejection of non-SELECT queries.
- Rejection of multiple statements.
- PostgreSQL read-only transactions for `execute_select()` queries, including those used by `list_tables()`.
- Environment configuration using `.env`.
- Credentials excluded from Git through the ignored `.env` file.
- 23 passing pytest tests.
- Integration verification against the real PostgreSQL 17 Docker container.

The automated tests cover client behavior; real-container integration verification was performed separately. Docker Compose currently provides the development database. The MCP server, Docker inspection tools, and Kubernetes integrations are not yet implemented.

## Roadmap

| Milestone | Scope | Status |
| --- | --- | --- |
| 1 | PostgreSQL client | Complete |
| 2 | MCP server and PostgreSQL MCP tools | Next |
| 3 | Docker inspection tools | Planned |
| 4 | Kubernetes inspection tools | Planned |
| 5 | Cross-system diagnostics | Planned |
| 6 | Security and production hardening | Planned |

The next MCP tools are expected to be:

- `postgres_health` — check PostgreSQL connectivity.
- `postgres_list_tables` — list public-schema base tables.
- `postgres_query` — execute a single SELECT query and return dictionary results.

These tools will expose the existing PostgreSQL client through MCP in Milestone 2.

## Current Project Structure

```text
opspilot-mcp/
├── README.md
├── pyproject.toml
├── compose.yaml
├── .env.example
├── .gitignore
├── src/
│   └── opspilot/
│       ├── __init__.py
│       ├── server.py                 # MCP server placeholder
│       ├── clients/
│       │   ├── __init__.py
│       │   ├── postgres.py           # Implemented PostgreSQL client
│       │   ├── docker.py             # Placeholder
│       │   └── kubernetes.py         # Placeholder
│       └── tools/
│           ├── __init__.py
│           ├── postgres.py           # PostgreSQL MCP tools placeholder
│           ├── docker.py             # Placeholder
│           └── kubernetes.py         # Placeholder
└── tests/
    ├── __init__.py
    └── test_postgres.py
```

## Local Development

Requirements: Python 3.11+ and Docker with Docker Compose.

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Edit `.env` to set your local PostgreSQL credentials before starting the database:

```bash
docker compose up -d
python -m pytest -v
```

Docker Compose starts a PostgreSQL 17 container named `opspilot-postgres`. The client reads `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` from the environment or `.env`; existing environment variables take precedence.

## Technology Stack

| Technology | Role |
| --- | --- |
| Python | Server and infrastructure client implementation |
| MCP | Planned protocol for exposing tools to AI assistants |
| PostgreSQL | First supported infrastructure service |
| Psycopg 3 | Synchronous PostgreSQL connectivity |
| Docker | Development database container; inspection integration planned |
| Docker Compose | Local PostgreSQL development environment |
| Kubernetes | Planned cluster inspection integration |
| pytest | Automated client tests |

## Security

`execute_select()` validates that input contains exactly one SELECT statement, rejecting non-SELECT queries and multiple statements before connecting. It then executes the query inside a PostgreSQL read-only transaction. `list_tables()` uses this same protected query path.

These protections limit database writes but are not a complete security boundary. Stronger database roles and permissions, including a dedicated least-privilege read-only role, will be added later as part of security and production hardening.

Local credentials belong in `.env`, which is excluded from Git. Keep `.env.example` limited to example values and never commit real credentials.

## Development Approach

```text
understand -> implement -> test -> verify -> commit -> next milestone
```

Each milestone starts with understanding the requirements, followed by implementation, automated tests, and verification against the relevant infrastructure before committing. Update this README as milestones and verification results change.
