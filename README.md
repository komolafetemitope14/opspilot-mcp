# OpsPilot MCP

OpsPilot MCP is a Python local infrastructure MCP server for inspecting PostgreSQL, Docker, and Kubernetes. It exposes curated observations, deterministic diagnostics, and Kubernetes/PostgreSQL cross-system correlation to AI assistants through the Model Context Protocol.

The supported deployment boundary is **trusted-local stdio**: a trusted local MCP client launches OpsPilot with shared infrastructure credentials. The project does not provide remote multi-user production security. An LLM is not required inside OpsPilot; an external AI client can reason over its structured results.

Milestones 1–5 (clients, MCP tools, and diagnostics) are complete. Milestone 6 security hardening is implemented through Slice 9; Slice 10 documents the implementation and separately completed live verification below. Remaining security and diagnostic work is listed under limitations.

## Architecture

```text
AI / MCP Client
    |
    | MCP / stdio
    v
OpsPilot MCP Server
    |
    v
 tools/  ----------------------> diagnostics/
    |                           deterministic interpretation
    v                                  |
 clients/                              v
    |                           cross-system correlation
    v                           (Kubernetes + PostgreSQL)
PostgreSQL / Docker / Kubernetes
```

`clients/` collect infrastructure facts and project them into curated observations. `diagnostics/` interpret normalized observations deterministically; correlation consumes normalized child diagnoses. `tools/` orchestrate these components and expose operations through MCP. Publication passes centralized redaction, with metadata-only completion auditing around tool calls.

The stack uses Python 3.11+, Psycopg 3, the Docker and Kubernetes Python SDKs, the MCP Python SDK, and pytest. Docker Compose supplies the local PostgreSQL 17 database; kind is the local Kubernetes demo environment.

## MCP tools

The current surface is exactly **17 tools**. Docker and Kubernetes tools are observational; there are no mutation or remediation tools.

| Tool | Parameters | Operation |
| --- | --- | --- |
| `postgres_health` | None | Check connectivity using `SELECT 1` |
| `postgres_list_tables` | None | List public-schema base tables |
| `postgres_query` | `query` | Execute one SELECT in a read-only transaction; return named rows |
| `docker_health` | None | Check Docker Engine connectivity |
| `docker_list_containers` | None | List running and stopped container summaries |
| `docker_inspect_container` | `name_or_id` | Return curated container state, health, exit code, and ports |
| `docker_container_logs` | `name_or_id`, `tail=100` | Retrieve bounded recent logs |
| `kubernetes_health` | None | Check core Kubernetes API discovery |
| `kubernetes_list_namespaces` | None | List namespace names |
| `kubernetes_list_pods` | `namespace=None` | List pods in one namespace or all namespaces |
| `kubernetes_inspect_pod` | `name`, `namespace="default"` | Inspect a pod's operational state |
| `kubernetes_pod_logs` | `name`, `namespace="default"`, `tail=100` | Retrieve bounded recent pod logs |
| `kubernetes_list_events` | `namespace=None` | List curated events in one namespace or all namespaces |
| `diagnose_postgres_issues` | None | Diagnose database health and public-table observations |
| `diagnose_docker_issues` | `name_or_id` | Diagnose one container |
| `diagnose_kubernetes_issues` | `name`, `namespace="default"` | Diagnose one pod and related events |
| `diagnose_cross_system_issues` | `name`, `namespace="default"` | Correlate that pod's diagnosis with PostgreSQL's diagnosis |

Log `tail` must be an integer from 1 to 1,000. Docker identifiers must be bare container names or IDs. Kubernetes log retrieval currently has no explicit container parameter; multi-container pods may require explicit selection support in future work.

## Local setup

Run commands from the repository root. Prerequisites:

- Python 3.11+ with virtual environment support.
- Docker Engine or Docker Desktop, Docker Compose, and permission to access the intended daemon.
- For Kubernetes: `kubectl`, a reachable cluster, and administrator credentials for provisioning. The local guide uses kind with cluster `opspilot` and administrator context `kind-opspilot`; install kind if creating that environment.
- Administrator PostgreSQL access for provisioning, kept separate from runtime reader access.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Edit the ignored `.env` locally. Supply `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_ADMIN_USER`, `POSTGRES_ADMIN_PASSWORD`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`. Use `opspilot_reader` for `POSTGRES_USER` and a distinct administrator identity. Set credentials privately; do not commit or paste them into demo output. Exported variables take precedence over dotenv values.

### PostgreSQL reader provisioning

```bash
docker compose up -d
# Wait for the database to become healthy before provisioning.
docker compose ps
sh scripts/provision-postgres-reader.sh
```

Compose runs PostgreSQL 17 in `opspilot-postgres` with a persistent named volume. Its administrator fields initialize the database; changing `.env` does not reset credentials in an existing database volume.

The provisioning script uses admin credentials to create/configure `opspilot_reader` as a login without superuser, database creation, role creation, replication, or RLS bypass attributes. It grants:

- CONNECT to the configured database.
- USAGE on schema `public`.
- SELECT on current tables in `public`.
- Default SELECT grants for future tables in `public` created by the configured `POSTGRES_ADMIN_USER` role.

Tables created by other owners need corresponding default privileges or explicit grants. Provisioning does **not** perform a comprehensive privilege reset: existing memberships, ownership, other grants, PUBLIC privileges, functions, and RLS behavior require deployment review.

Runtime uses `POSTGRES_USER=opspilot_reader` and its password. Admin credentials are provisioning-only (also used by Compose to initialize the development database); runtime connections never fall back to admin fields. Keep admin credentials out of the runtime launch environment and runtime dotenv configuration. A shared `.env` can still expose them to the process even though they are not used for runtime connections. Preserve them separately for later provisioning.

The application also validates a single SELECT statement and uses read-only transactions. **PostgreSQL is the actual authorization boundary**; SQL screening is an additional application control, not a substitute for effective database privileges.

A fresh Compose database may contain no public application tables. Table discovery can then return an empty list and PostgreSQL diagnosis can report `degraded` with `no_public_tables`; a healthy database diagnosis in the recorded demo reflects its existing tables.

### Kubernetes reader provisioning: ordering matters

If needed, create the local cluster:

```bash
kind create cluster --name opspilot
```

Provision using the administrator configuration, before selecting the reader for runtime. In particular, an exported reader `KUBECONFIG` does not contain the administrator context.

```bash
unset KUBECONFIG
kubectl config use-context kind-opspilot
kubectl config current-context
# Confirm the output is kind-opspilot before continuing.
kubectl --context kind-opspilot get namespaces
sh scripts/provision-kubernetes-reader.sh --admin-context kind-opspilot
python scripts/verify-kubernetes-reader.py \
  --kubeconfig "$PWD/.local/kubernetes/opspilot-reader.kubeconfig"
# Only after successful verification, select the reader for OpsPilot runtime.
export KUBECONFIG="$PWD/.local/kubernetes/opspilot-reader.kubeconfig"
```

The script applies [the RBAC manifest](kubernetes/opspilot-reader-rbac.yaml): a dedicated `opspilot-reader` ServiceAccount in `opspilot-system`, ClusterRole, and ClusterRoleBinding. Its observation permissions are list namespaces; get/list pods; get pod logs; list events; and get `/api/v1`. It grants no Secret access, workload mutation, exec, attach, or port-forward permissions. Additional bindings can expand effective authority, so run the verifier against the real cluster. The verifier uses identity/access reviews and six observational read paths; it relies on the cluster's review permissions.

The generated standalone, token-only kubeconfig is `.local/kubernetes/opspilot-reader.kubeconfig`. Git ignores `.local/kubernetes/`; provisioning sets directory/file modes to **0700/0600** and atomically replaces the file. Protect parent directories and the local account as well.

Credentials are short-lived: provisioning requests a one-hour token, with actual lifetime controlled by the API server. There is currently **no automatic token renewal**. To renew, repeat the administrator-first sequence above, verify the new reader, then relaunch OpsPilot with the generated kubeconfig. An expired reader token can produce **HTTP 401 (authentication failure)**; do not confuse it with an RBAC authorization denial, typically HTTP 403.

The runtime validates static kubeconfig credentials and HTTPS configuration; executable credential plugins and auth-provider plugins are unsupported. Configuration validation does not attest effective identity or RBAC.

### Docker access

Ensure the local account can reach the intended daemon. OpsPilot resolves Docker environment/context settings, including `DOCKER_HOST`, `DOCKER_CONTEXT`, and applicable TLS settings. Review the selected endpoint and local socket permissions.

OpsPilot exposes observational Docker operations only and no mutation tools. **Access to an unrestricted Docker daemon/socket is not daemon-enforced read-only access.** Compromised code in the process could use daemon authority directly, potentially reaching host-level authority. Trusted local deployment remains part of the security boundary.

## Start with an MCP client

Canonical startup, with the virtual environment active:

```bash
python -m opspilot.server
```

This is an MCP stdio process, normally launched and managed by an MCP client. For client configuration, use the absolute virtualenv Python executable so interpreter selection does not depend on shell activation:

```json
{
  "mcpServers": {
    "opspilot": {
      "command": "/absolute/path/opspilot-mcp/.venv/bin/python",
      "args": ["-m", "opspilot.server"]
    }
  }
}
```

Replace the path with your checkout's absolute path. Configure the client's working directory as the repository root and provide its runtime PostgreSQL configuration and generated reader `KUBECONFIG` through the client's supported environment mechanism. GUI clients may not inherit your terminal's environment. Client configuration formats vary; the executable and arguments above are the required launch values.

Supported module startup explicitly selects stdio. There is no supported remote HTTP deployment. Stdio does not authenticate callers or provide per-user authorization; the local launcher and MCP client must be trusted.

## Diagnostic semantics

Diagnostics return `status`, `system`, resource identity, and `findings`. Findings contain `code`, `severity`, `summary`, `evidence`, and `recommendation`. Rules are deterministic for the same supplied observations:

- `healthy`: no implemented finding rule fired.
- `degraded`: warnings exist without errors.
- `unhealthy`: at least one error finding exists.

PostgreSQL rules cover failed health checks and absence of public tables. Docker rules cover non-running containers, nonzero exit codes, and unhealthy/starting health state. Kubernetes rules cover pod phase/readiness, restarts, image pull failures, crash loops, and Warning events; related image-pull evidence is aggregated.

Cross-system correlation currently covers **Kubernetes + PostgreSQL**. When both child diagnoses are `unhealthy`, it emits `possible_database_dependency_failure`. Otherwise, the current rule returns `healthy` with no findings. **A healthy cross-system result only means no implemented correlation rule fired; it does not certify every child system healthy.** Correlation is not causation and does not prove that the pod depends on PostgreSQL.

Backend/connectivity exceptions may propagate as tool failures rather than becoming diagnostic findings. A health response, a diagnostic status, and successful tool execution are distinct signals; audit success does not assert infrastructure health.

## Security hardening and boundary

See the maintained [threat model](docs/threat-model.md) for assumptions, mechanisms, evidence, and residual risks.

Implemented controls include:

- Centralized post-conversion secret redaction for registered tool publication, with recognized credential forms and registered secret values; curated infrastructure projections reduce exposed fields.
- Lazy, validated runtime configuration before backend activation, no PostgreSQL admin fallback, and a static Kubernetes configuration snapshot.
- Native backend timeouts, PostgreSQL streamed rows, bounded listings/pagination, log retrieval, values, observations, and diagnostic evidence.
- Best-effort metadata-only audit events with tool/outcome/failure category and correlation metadata; SQL, observations, and exception text are excluded.
- Startup hardening that installs owned redacting stderr and audit handlers idempotently. Import/tool discovery does not activate infrastructure or install owned runtime logging.
- Explicit stdio selection on canonical module startup.

Current policy includes 500 query rows/list items, log tails up to 1,000 lines, 256 KiB log payloads, and a 1 MiB logical observation budget. PostgreSQL uses a 5-second connection timeout and 10-second statement timeout; Docker uses a 10-second native request timeout; Kubernetes uses 5/10-second connect/read timeouts. These are practical retrieval/output policies, not hard total deadlines, process-memory limits, or serialized MCP wire-size ceilings. SDKs may buffer data before checks.

Redaction does not identify arbitrary sensitive business data or every unknown secret. Direct stderr writes, independent handlers, pre-runtime failures, and some SDK/protocol paths remain outside comprehensive protection. Audit delivery can fail or be lost on abrupt termination, and stderr can block. Local credentials, dependencies, administrators, backends, and the launcher remain trusted. Runtime validation does not prove live PostgreSQL privileges or Kubernetes RBAC.

## Verification and local demo

### Recorded evidence

The previous completed security slice reported **751 automated tests passed**. This is regression evidence for application behavior; it does **not** prove live infrastructure authorization. These tests and the following live checks were completed before this README update; the documentation task does not claim a fresh run.

Separately completed live verification supplied for Milestone 6, Slice 10:

| Area | Observed result |
| --- | --- |
| PostgreSQL reader | Health, table discovery, and SELECT succeeded; database-enforced INSERT, UPDATE, DELETE, and DROP attempts were denied with SQLSTATE `42501` |
| Kubernetes reader | Provisioning/verifier passed 97 authorization checks and six live read checks; `create deployments.apps` was denied |
| Docker | Observational calls succeeded; bounded Docker log retrieval was demonstrated |
| MCP discovery | Canonical stdio startup discovered all 17 tools: expected 17, discovered 17, missing 0, unexpected 0 |
| MCP invocation | Representative PostgreSQL, Docker, Kubernetes, and diagnostic tools succeeded; bounded Kubernetes logs were demonstrated |
| SQL application rejection | MCP `postgres_query` accepted `SELECT 1`; a DELETE was rejected by the application before database execution |
| Failure handling | The MCP-facing failure was generic; the underlying SDK emitted an internal traceback to stderr. The audit event contained metadata/failure category without the SQL query |
| Live diagnoses | A real exited Docker container produced `degraded` with `container_not_running`; PostgreSQL and Kubernetes diagnoses were healthy; cross-system correlation returned healthy with no findings |

A **separate synthetic deterministic test** supplied unhealthy Kubernetes and PostgreSQL diagnoses and produced `possible_database_dependency_failure`. This was synthetic correlation input: **no infrastructure was intentionally broken** for this verification. The healthy live correlation and synthetic positive case demonstrate different paths; neither establishes a real database-caused pod failure.

### Reproduce a local walkthrough

After setup and reader verification, launch OpsPilot from your MCP client with the canonical executable/arguments above:

1. Request tool discovery and compare the names with the 17-tool table.
2. Call `postgres_health`, `postgres_list_tables`, and `postgres_query` with `{"query":"SELECT 1"}`.
3. Call `docker_health` and `docker_list_containers`; choose an existing container's name/ID for inspection, `docker_container_logs` with `tail=20`, and `diagnose_docker_issues`.
4. Call `kubernetes_health`, `kubernetes_list_namespaces`, and `kubernetes_list_pods`. Choose an existing pod and its namespace for inspection, `kubernetes_pod_logs` with `tail=20`, and `kubernetes_list_events` for that namespace.
5. Call `diagnose_postgres_issues`, `diagnose_kubernetes_issues`, and `diagnose_cross_system_issues` for the selected pod. Interpret the results using the semantics above; cluster contents and database tables affect findings.
6. To demonstrate application SQL validation, pass `{"query":"DELETE FROM opspilot_demo_validation_only"}` to `postgres_query`. It should be rejected before execution and does not require that table to exist. This checks application rejection, not database authorization.

Run the existing offline regression suite separately:

```bash
python -m pytest -v
```

For effective authorization evidence, use the Kubernetes verifier and separately review/test the deployed PostgreSQL reader's effective privileges. Do not infer database-enforced denial from an application-rejected DELETE or infer live RBAC from mocked tests. The recorded 751-test count and live results above are historical evidence, not promised output for every local environment.

## Repository guide

| Path | Purpose |
| --- | --- |
| `src/opspilot/server.py` | MCP registration and canonical stdio startup |
| `src/opspilot/tools/` | PostgreSQL, Docker, Kubernetes, and diagnostic tools |
| `src/opspilot/clients/` | Backend observation clients |
| `src/opspilot/diagnostics/` | Deterministic per-system rules and correlation |
| `src/opspilot/configuration.py` | Validated backend settings |
| `src/opspilot/redaction.py`, `audit.py`, `limits.py` | Publication, audit, and observation policies |
| `scripts/` | Reader provisioning and Kubernetes live verification |
| `kubernetes/opspilot-reader-rbac.yaml` | Narrow reader RBAC policy |
| `compose.yaml`, `.env.example` | Local database setup and configuration template |
| `tests/` | Automated regression coverage |
| `docs/threat-model.md` | Security model and residual risks |

## Limitations and future work

- Remote authentication/HTTP transport, transport security policy, and per-user/per-resource authorization remain future work. Inherited SDK transport capabilities do not make remote hosting supported.
- Stronger Docker isolation or an allowlisted authorization proxy is needed to restrict daemon authority independently of application tools.
- Durable, tamper-evident audit storage, stronger delivery guarantees, and authenticated attribution are not implemented.
- Rate/concurrency controls, aggregate-load limits, hard total deadlines, memory ceilings, and serialized wire-size ceilings remain open.
- Broader sensitive-data detection and dependency/observation provenance require further work; deterministic findings do not validate the truth or origin of backend evidence.
- Kubernetes token renewal is manual. Explicit container selection is needed where pod log/diagnostic behavior depends on a particular container.
- Richer dependency mapping and diagnostics, including Docker cross-system correlation, remain future work.
- Backend failures need more normalized diagnostic handling. Cleanup errors can currently mask an original operation failure and affect diagnosis/audit classification; cleanup error precedence remains unresolved.
