# OpsPilot MCP

OpsPilot MCP is a Python-based local infrastructure MCP server that allows AI assistants to inspect PostgreSQL, Docker, and Kubernetes through the Model Context Protocol (MCP).

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

The MCP server, PostgreSQL tools, read-only Docker and Kubernetes inspection tools, and deterministic Kubernetes diagnostics are implemented:

```text
AI / MCP Client
       |
       | MCP
       v
OpsPilot MCP Server
       |
       +---- PostgreSQL (implemented)
       +---- Docker (implemented, read-only)
       +---- Kubernetes (implemented, read-only)
       +---- diagnostics/ (deterministic Kubernetes diagnostics)
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

Real-container verification was performed separately from the automated tests. Docker Compose currently provides the development database. Cross-system diagnostics remain planned.

**Milestone 4 — Kubernetes Inspection Tools: COMPLETE**

Implemented and verified:

- Synchronous `KubernetesClient` using the Kubernetes Python client and kubeconfig/current context.
- Kubernetes API health check and namespace listing.
- Pod listing across all namespaces or a selected namespace.
- Pod inspection with selected operational details.
- Pod log retrieval with `tail` validated as a positive integer.
- Kubernetes Event listing across all namespaces or a selected namespace.
- Curated JSON-friendly results rather than raw SDK objects.
- Read-only inspection design; Kubernetes write operations and remediation are not implemented.

The implemented MCP server exposes six read-only Kubernetes tools:

- `kubernetes_health` — checks communication with the Kubernetes API.
- `kubernetes_list_namespaces` — lists namespace names.
- `kubernetes_list_pods` — lists Pod summaries across all namespaces or a selected namespace.
- `kubernetes_inspect_pod` — returns selected operational details for a named Pod.
- `kubernetes_pod_logs` — retrieves recent Pod log lines with positive `tail` validation.
- `kubernetes_list_events` — returns curated Event summaries across all namespaces or a selected namespace.

At Milestone 4 completion, OpsPilot exposed 13 MCP inspection tools: 3 PostgreSQL, 4 Docker, and 6 Kubernetes tools.

Verification completed:

- 53 Kubernetes tests passed.
- 65 MCP tests passed.
- 164 tests passed in the full suite.
- Real Kubernetes integration was tested against the local kind cluster named `opspilot`, using the `kind-opspilot` context.
- Real end-to-end MCP -> OpsPilot -> Kubernetes calls succeeded for all six Kubernetes tools.
- A diagnostic event experiment used a deliberately invalid nginx image to produce `ImagePullBackOff`. OpsPilot successfully retrieved the resulting Kubernetes Events, and the temporary failing Pod was deleted afterward. Events are transient diagnostic data, not permanent records.

Real-cluster verification was performed separately from the automated tests.

**Milestone 5 — Cross-System Diagnostics: IN PROGRESS**

The first Kubernetes diagnostics vertical slice is implemented and verified. OpsPilot now exposes 14 MCP tools total: 3 PostgreSQL inspection tools, 4 Docker inspection tools, 6 Kubernetes inspection tools, and 1 diagnostic tool:

- `diagnose_kubernetes_issues` — analyzes a named Pod's state and related Kubernetes Events using the deterministic `KubernetesDiagnostics` engine under `src/opspilot/diagnostics/`.

Structured diagnostic results include:

- Overall `status` (`healthy`, `degraded`, or `unhealthy`).
- `system` and resource identity (`kind`, `name`, and `namespace`).
- `findings` with `code`, `severity`, `summary`, `evidence`, and `recommendation`.

Current diagnostic rules cover:

- Pod not running.
- Pod not ready.
- Container restarts.
- Image pull failures, including `ImagePullBackOff` and `ErrImagePull`.
- `CrashLoopBackOff`.
- Generic Kubernetes Warning events.

Multiple image-pull observations are correlated into one `image_pull_failure` finding with aggregated evidence.

The architecture separates responsibilities: `clients/` collect infrastructure observations, `diagnostics/` interpret and correlate those observations, and `tools/` expose functionality through MCP. The diagnostic engine is deterministic; an LLM is not required inside OpsPilot. An external AI/MCP client can reason over the structured diagnostic results.

Verification completed:

- 177 tests pass in the current automated suite.
- Real end-to-end verification against the local kind cluster succeeded through the full path: MCP Client -> OpsPilot MCP Server -> `diagnose_kubernetes_issues` -> `KubernetesClient` -> real Kubernetes Pod/Events -> `KubernetesDiagnostics` -> structured MCP response.
- A deliberately broken test Pod used `nginx:this-tag-does-not-exist` and reached `ImagePullBackOff`.
- The resulting MCP diagnosis correctly returned status `unhealthy` and an `image_pull_failure` finding.
- The broken test Pod was deleted after verification.
- The normal `opspilot-demo` workload remains healthy with two running replicas.

Docker diagnostics, PostgreSQL diagnostics, and cross-system correlation remain future Milestone 5 work. Milestone 6 security and production hardening remains planned.

## Roadmap

| Milestone | Scope | Status |
| --- | --- | --- |
| 1 | PostgreSQL client | Complete |
| 2 | MCP server and PostgreSQL MCP tools | Complete |
| 3 | Docker inspection tools | Complete |
| 4 | Kubernetes inspection tools | Complete |
| 5 | Cross-system diagnostics | In progress |
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
│       │   └── kubernetes.py         # Implemented read-only Kubernetes client
│       ├── diagnostics/
│       │   ├── __init__.py
│       │   └── kubernetes.py         # Deterministic Kubernetes diagnostic engine
│       └── tools/
│           ├── __init__.py
│           ├── postgres.py           # Implemented PostgreSQL MCP tools
│           ├── docker.py             # Implemented read-only Docker MCP tools
│           ├── kubernetes.py         # Implemented read-only Kubernetes MCP tools
│           └── diagnostics.py        # Implemented Kubernetes diagnostic MCP tool
└── tests/
    ├── __init__.py
    ├── test_postgres.py
    ├── test_docker.py                # DockerClient unit tests
    ├── test_kubernetes.py            # KubernetesClient unit tests
    ├── test_mcp.py                   # PostgreSQL, Docker, and Kubernetes MCP tool tests
    ├── test_diagnostic_tools.py      # Diagnostic MCP tool tests
    └── test_kubernetes_diagnostics.py # Kubernetes diagnostic engine tests
```

## Local Development

Requirements: Python 3.11+ and Docker with Docker Compose. Kubernetes inspection also requires access to a cluster through kubeconfig; the client uses the current context. Local Kubernetes verification used kind with a cluster named `opspilot` and context `kind-opspilot`.

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

The server communicates with an MCP client over standard input and output and exposes the 14 inspection and diagnostic tools listed above. Direct stdio server/client communication, manual MCP-to-Docker communication, and real end-to-end MCP -> OpsPilot -> Kubernetes calls for all six Kubernetes tools have been verified.

## Technology Stack

| Technology | Role |
| --- | --- |
| Python | Server and infrastructure client implementation |
| MCP | Implemented server protocol exposing PostgreSQL, Docker, and Kubernetes tools to AI assistants |
| PostgreSQL | First supported infrastructure service |
| Psycopg 3 | Synchronous PostgreSQL connectivity |
| Docker | Development database container and implemented read-only inspection integration |
| Docker SDK for Python | Docker API client |
| Docker Compose | Local PostgreSQL development environment |
| Kubernetes | Implemented read-only cluster inspection integration |
| Kubernetes Python client | Kubernetes API access using kubeconfig/current context |
| kind | Local Kubernetes integration verification |
| pytest | Automated PostgreSQL, Docker, Kubernetes, and MCP tool tests |

## Security

`execute_select()` validates that input contains exactly one SELECT statement, rejecting non-SELECT queries and multiple statements before connecting. It then executes the query inside a PostgreSQL read-only transaction. `list_tables()` uses this same protected query path.

These protections limit database writes but are not a complete security boundary. Stronger database roles and permissions, including a dedicated least-privilege read-only role, will be added later as part of security and production hardening.

Local credentials belong in `.env`, which is excluded from Git. Keep `.env.example` limited to example values and never commit real credentials.

## Development Approach

```text
understand -> implement -> test -> verify -> commit -> next milestone
```

Each milestone starts with understanding the requirements, followed by implementation, automated tests, and verification against the relevant infrastructure before committing. Update this README as milestones and verification results change.
