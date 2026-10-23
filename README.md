# OpsPilot MCP

OpsPilot MCP is a Python-based local infrastructure MCP server that allows AI assistants to inspect PostgreSQL and Docker through the Model Context Protocol (MCP). Kubernetes integration remains planned for a later milestone.

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

The MCP server, PostgreSQL tools, and read-only Docker inspection tools are implemented. Kubernetes integration remains planned:

```text
AI / MCP Client
       |
       | MCP
       v
OpsPilot MCP Server
       |
       +---- PostgreSQL (implemented)
       +---- Docker (implemented, read-only)
       +---- Kubernetes (planned)
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
- Integration verification against the real PostgreSQL 17 Docker container.

**Milestone 2 — MCP server and PostgreSQL MCP tools: COMPLETE**

The implemented MCP server exposes three PostgreSQL tools:

- `postgres_health` — checks PostgreSQL connectivity.
- `postgres_list_tables` — lists public-schema base tables.
- `postgres_query` — executes one SELECT statement using the existing read-only PostgreSQL client path and returns dictionary results.

Verification completed:

- At Milestone 2 completion, 38 pytest tests passed, covering PostgreSQL client behavior and MCP tools.
- MCP tool discovery was tested.
- stdio MCP server/client communication was tested.
- Manual end-to-end verification against the real PostgreSQL Docker container succeeded: `postgres_health` returned `true`, `postgres_list_tables` returned `["services"]`, and `postgres_query` returned the three existing service records.
- A DELETE sent through `postgres_query` was rejected and returned an MCP tool error.

**Milestone 3 — Docker Inspection Tools: COMPLETE**

The implemented MCP server exposes four read-only Docker tools:

- `docker_health` — checks communication with the Docker Engine.
- `docker_list_containers` — lists running and stopped containers using curated JSON-friendly data.
- `docker_inspect_container` — returns selected operational details, including status, health, running state, exit code, and ports.
- `docker_container_logs` — returns a bounded number of recent container log lines.

Docker operations are currently read-only. OpsPilot does not start, stop, restart, remove, or create containers.

Verification completed:

- 83 pytest tests pass across the PostgreSQL, Docker, and MCP functionality.
- 23 DockerClient unit tests were added.
- DockerClient was manually verified against the real Docker Desktop engine.
- `docker_health` returned `true`.
- Container listing found both running and exited containers.
- Inspection of `opspilot-postgres` returned healthy/running state and port information.
- Recent PostgreSQL container logs were retrieved successfully.
- Manual MCP-to-Docker end-to-end verification succeeded.
- Invalid `tail=0` produced an MCP tool error.
- Inspecting a nonexistent container produced an MCP tool error.

Real-container verification was performed separately from the automated tests. Docker Compose currently provides the development database. Kubernetes inspection and cross-system diagnostics remain planned.

## Roadmap

| Milestone | Scope | Status |
| --- | --- | --- |
| 1 | PostgreSQL client | Complete |
| 2 | MCP server and PostgreSQL MCP tools | Complete |
| 3 | Docker inspection tools | Complete |
| 4 | Kubernetes inspection tools | Next |
| 5 | Cross-system diagnostics | Planned |
| 6 | Security and production hardening | Planned |

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
│       ├── server.py                 # Implemented MCP server
│       ├── clients/
│       │   ├── __init__.py
│       │   ├── postgres.py           # Implemented PostgreSQL client
│       │   ├── docker.py             # Implemented Docker client
│       │   └── kubernetes.py         # Placeholder
│       └── tools/
│           ├── __init__.py
│           ├── postgres.py           # Implemented PostgreSQL MCP tools
│           ├── docker.py             # Implemented read-only Docker MCP tools
│           └── kubernetes.py         # Placeholder
└── tests/
    ├── __init__.py
    ├── test_postgres.py
    ├── test_docker.py                # DockerClient unit tests
    └── test_mcp.py                   # PostgreSQL and Docker MCP tool tests
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

### Local MCP Usage

With the virtual environment active, start the MCP server from the repository root:

```bash
mcp run src/opspilot/server.py:mcp --transport stdio
```

The server communicates with an MCP client over standard input and output and exposes `postgres_health`, `postgres_list_tables`, `postgres_query`, `docker_health`, `docker_list_containers`, `docker_inspect_container`, and `docker_container_logs`. Direct stdio server/client communication and manual MCP-to-Docker end-to-end communication have been verified.

## Technology Stack

| Technology | Role |
| --- | --- |
| Python | Server and infrastructure client implementation |
| MCP | Implemented server protocol exposing PostgreSQL and Docker tools to AI assistants |
| PostgreSQL | First supported infrastructure service |
| Psycopg 3 | Synchronous PostgreSQL connectivity |
| Docker | Development database container and implemented read-only inspection integration |
| Docker SDK for Python | Docker API client |
| Docker Compose | Local PostgreSQL development environment |
| Kubernetes | Planned cluster inspection integration |
| pytest | Automated PostgreSQL client, DockerClient, and MCP tool tests |

## Security

`execute_select()` validates that input contains exactly one SELECT statement, rejecting non-SELECT queries and multiple statements before connecting. It then executes the query inside a PostgreSQL read-only transaction. `list_tables()` uses this same protected query path.

These protections limit database writes but are not a complete security boundary. Stronger database roles and permissions, including a dedicated least-privilege read-only role, will be added later as part of security and production hardening.

Local credentials belong in `.env`, which is excluded from Git. Keep `.env.example` limited to example values and never commit real credentials.

## Development Approach

```text
understand -> implement -> test -> verify -> commit -> next milestone
```

Each milestone starts with understanding the requirements, followed by implementation, automated tests, and verification against the relevant infrastructure before committing. Update this README as milestones and verification results change.
