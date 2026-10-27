# OpsPilot security threat model

## 1. Purpose and scope

OpsPilot is designed for a trusted local launcher using MCP stdio. It exposes
infrastructure observations and deterministic diagnostics through a shared runtime
identity. Remote hosting of the inherited MCP object is technically possible but
unsupported. Stdio does not authenticate an MCP caller.

Labels distinguish the strength of each statement:

- **GUARANTEE**: behavior enforced by OpsPilot code or source-controlled policy,
  within the stated scope and assumptions.
- **ASSUMPTION**: an external condition that must be trusted or verified.
- **LIMITATION**: a protection OpsPilot does not enforce.
- **DEFERRED**: work needed for a different deployment or stronger policy.

This document is the maintained security model. Test names are evidence for
specific mechanisms, not proof of deployed infrastructure state. Test counts are
not security evidence.

## 2. System model

```text
MCP client / launcher
        |
        | stdio
        v
OpsPilot MCP server
        |
        +--> PostgreSQL
        +--> Docker
        +--> Kubernetes
```

The server registers three PostgreSQL tools, four Docker tools, six Kubernetes
tools and four diagnostic/correlation tools.

```text
MCP request
  → AuditMiddleware (outermost)
  → SDK middleware (OpenTelemetryMiddleware, RequestStateBoundary)
  → RedactionMiddleware
  → MCP dispatch / argument validation
  → tool
  → client / diagnostics
  → backend
  → bounded result / diagnostic evidence
  → tool-result budget check
  → SDK result conversion
  → redaction
  → response returned through outer middleware
  → completion audit attempt
  → SDK transport writes response
```

Diagnostics consume raw observations before publication redaction. For diagnostics,
backend observations precede analysis; correlation can perform multiple backend
operations. Audit is outermost so SDK request-state rejection is included. The
completion attempt occurs during middleware unwinding, before transport delivery;
an audit success does not prove delivery or infrastructure health.

**GUARANTEE:** module execution explicitly selects `transport="stdio"`.
Both module execution and SDK CLI import/run reach the exported server's `run()`
boundary, which installs owned safe stderr and audit handlers before delegating
to the inherited SDK runner. Repeated setup replaces handlers rather than adding
duplicates. Import/discovery does not invoke this owned logging setup.

**LIMITATION:** the SDK itself may configure default logging when constructing a
server. OpsPilot does not configure logging merely on import, but does not promise
that third-party imports have no logging effects in an otherwise unconfigured
process. Runtime setup replaces root handlers for the dedicated server process.

**LIMITATION:** redaction is inside SDK middleware. It does not wrap every possible
SDK, transport, parser, protocol-envelope or direct stderr path. Installed SDK
stdio framing/stray-output isolation is a dependency, not an OpsPilot sandbox.

## 3. Assets

C/I/A describe confidentiality, integrity and availability requirements.

| Asset | C | I | A | Holder/accessor |
| --- | --- | --- | --- | --- |
| PostgreSQL data | Sensitive business data | Prevent unintended mutation | Avoid disruptive queries | Database, client, caller |
| Runtime PostgreSQL credentials | Secret | Select intended runtime role | Needed for observation | Environment/dotenv, settings, psycopg |
| Provisioning/admin credentials | Highly privileged secret | Keep provisioning authority separate | Needed during provisioning | Operator, Compose, provisioning process |
| Kubernetes API data | Sensitive workload/topology data | Accurate observations | Bounded API use | API server, Kubernetes client |
| ServiceAccount token | Secret | Intended identity and audience | Expiry/renewal affects access | Kubeconfig, runtime memory |
| Generated kubeconfig | Secret token and connection data | Protect cluster/CA/context | Readable and renewed | `.local/kubernetes/`, loader |
| Docker metadata | Sensitive names/images/ports | Accurate projections | Bounded operations | Daemon, SDK, caller |
| Docker logs | Potentially sensitive | Treat as untrusted content | Bound retrieval/output | Daemon, SDK buffers, output |
| Docker daemon authority/socket | Critical capability | Prevent unauthorized commands | Protect daemon/host | OS identity, SDK session |
| Infrastructure topology | Operationally sensitive | Avoid misleading conclusions | Available for diagnosis | Projections, diagnostic results |
| Diagnostic evidence | May contain secrets | Preserve intended interpretation | Bound evidence expansion | Diagnostic engines/tools |
| MCP outputs | Protect recognized credentials | Correct results and sanitization | Practical output bounds | SDK, middleware, caller |
| Runtime configuration | Credentials/endpoints sensitive | Intended backend and authority | Valid configuration required | Launcher, files, loaders |
| Audit records | Target/correlation metadata sensitive | Accurate bounded metadata | Best-effort delivery | Middleware, stderr collector |
| Stderr | May contain credentials | Safe operational records | Consumer must drain pipe | Logging handlers, launcher |
| MCP stdout | Tool data sensitive | Valid uncontaminated frames | Usable pipe | SDK transport, launcher |
| Local process identity | Grants files/socket access | Trusted execution | Process remains viable | Local OS and launcher |
| Provisioning policy | Usually public | Reviewed grants/RBAC | Reproducible provisioning | Source-controlled scripts/manifests |

## 4. Trust boundaries

| ID | Boundary | Trusted side / enforcement | Not guaranteed |
| --- | --- | --- | --- |
| B1 | Launcher/client → OpsPilot | Trusted launcher controls pipes; schemas and narrow tool surface | Authenticated caller, per-user access, hostile-client isolation |
| B2 | OpsPilot → PostgreSQL | Intended endpoint and database permissions; explicit runtime kwargs and read-only query transaction | Effective least privilege, function safety, uniform TLS policy |
| B3 | OpsPilot → Kubernetes | Intended cluster/CA/static credentials; HTTPS and static snapshot validation | Runtime identity is provisioned SA; no extra effective grants |
| B4 | OpsPilot → Docker | Trusted endpoint/context and daemon; fixed observational methods | Daemon-enforced read-only authority |
| B5 | Provisioning identity → runtime identity | Trusted administrator; separate fields, policy, token-only kubeconfig | Complete revocation of existing grants; runtime cannot read inherited admin secrets |
| B6 | Backend observation → diagnostics | Backend text treated as data; projections, budgets, deterministic rules | Truthfulness or downstream prompt-injection resistance |
| B7 | Internal result → redaction → publication | Central post-conversion tool-result redaction; fail-closed policy | Arbitrary-secret detection or every SDK-generated response |
| B8 | OpsPilot → audit/stderr | Owned handlers at runtime; metadata allowlist and redaction | Durable, authenticated, tamper-evident or nonblocking records |
| B9 | Config source → validation → activation | Trusted local owner; supported-field validation and immutable snapshots | Arbitrary endpoint/identity is safe; all ambient SDK behavior constrained |
| B10 | Third-party SDK → OpsPilot assumptions | Trusted installed dependencies; targeted contract tests | Compromised SDK/dependency or identical behavior across all compatible versions |

## 5. Security control matrix: M6 Slices 1–8

Test paths are relative to the repository's `tests/` directory.

| Slice / claim | Production mechanism | Test/evidence | Not proven | Residual limitation |
| --- | --- | --- | --- | --- |
| 1: PostgreSQL reader policy and separate runtime credentials | Provision `opspilot_reader` with restricted role attributes and SELECT grants; explicit runtime config; SELECT screening; read-only transaction | `test_postgres_provisioning.py::test_provisions_new_and_existing_role`; `test_configuration.py::test_postgres_admin_never_substitutes`; `test_postgres.py::test_select_uses_read_only_transaction`, `test_rejects_invalid_queries_before_connecting` | Effective deployed privileges, memberships, ownership, function permissions or RLS | Existing authority is not fully revoked; allowed SELECT exposes granted data |
| 2: Kubernetes observational identity policy | Dedicated SA; exact ClusterRole/binding; requested one-hour TokenRequest; private token-only kubeconfig; identity/access-review verifier | `test_kubernetes_provisioning.py::test_exact_rbac_matrix_and_subject`, `test_standalone_generation_and_atomic_private_output`, `test_verification_uses_reviews_never_workload_mutations`, `test_verifier_rejects_excess_permission_or_evaluation_error` | Live effective RBAC, actual token lifetime, uncompromised control plane | Extra bindings can expand access; allowed logs/events can contain secrets |
| 3: Docker application surface is observational | Fixed MCP tools/SDK operations, bare identifier validation, curated projection | `test_mcp.py::test_docker_mcp_surface_is_exactly_observational`; `test_docker.py::test_operation_closes_sdk_and_only_observes`, `test_sensitive_inspection_fields_are_projected_out`, `test_diagnostic_tool_sdk_calls_are_inspection_only` | Read-only socket/daemon authority or compromise containment | Process still has unrestricted configured daemon authority |
| 4: Central recognized-secret publication policy | Recursive post-conversion redaction; credential forms/registry; redacting formatter and safe handler | `test_redaction.py::test_text_forms`, `test_nested_copy_and_operational_values`, `test_fail_closed_formatter_and_boundary`, `test_owned_handler_never_dumps_raw_diagnostics`; `test_mcp_redaction.py::test_successful_wire_output`, `test_diagnostics_use_raw_events_then_publish_safe_evidence` | Arbitrary-secret detection, direct writes, every SDK path | Unknown sensitive values pass; independent handlers can bypass policy |
| 5: Local validation before backend activation | Lazy constant-error loaders; no admin fallback; Kubernetes HTTPS/static projection and file snapshot; Docker settings validation | `test_configuration.py`; `test_docker.py::test_configuration_failure_prevents_client_and_transport_construction`; `test_kubernetes.py::test_activation_uses_snapshot_after_source_material_changes`; `test_diagnostic_tools.py::test_cross_system_validates_both_before_activation` | Effective identity/privileges; universal endpoint policy | Trusted configuration may select powerful identities/endpoints; ambient SDK settings remain |
| 6: Practical observation/time policies | Native timeouts, streaming DB rows, item/value/log/aggregate budgets, bounded pagination and diagnostic evidence | `test_limits.py`; `test_postgres.py::test_overflow_closes_before_stream_finalizer_and_never_drains`, `test_installed_psycopg_stream_finalizer_skips_drain_after_close`; `test_docker.py::test_timeout_is_active_inside_real_sdk_version_negotiation`; `test_kubernetes.py::test_endless_unique_empty_pages_are_bounded`; `test_kubernetes_diagnostics.py::test_grouped_evidence_cannot_bypass_bound` | Hard memory, total deadline, aggregate load or serialized wire-size caps | SDK buffering and repeated/concurrent calls remain |
| 7: Best-effort metadata-only completion audit | Outermost middleware; static policy; safe categories; UUID; ContextVar; safe JSON handler | `test_mcp_audit.py::test_all_tools`, `test_real_conversion_category`, `test_stdio_audit_has_no_authenticated_caller_identity`, `test_request_state_rejections_are_audited`, `test_output_sanitization_failure`; `test_audit.py::test_fail_open`, `test_concurrent_context_and_cancellation` | Durable/authenticated records, abrupt-death completion, nonblocking sink | Setup/handler failure can drop events; crash before completion leaves no record |
| 8: Supported module deployment selects stdio | Explicit module run argument; lazy discovery | `test_mcp.py::test_supported_module_startup_is_stdio_only`, `test_fresh_server_import_and_discovery_without_backend_activation`, `test_stdio_module_startup_and_discovery` | Remote hosting is impossible | Inherited SDK remote capability exists but is unsupported |

Slice 9 startup evidence: `test_startup_security.py::test_startup_installs_owned_handlers_idempotently`
and `test_startup_framework_failure_is_redacted_and_audited` exercise both module
and installed SDK CLI startup composition. They protect registered/recognized
synthetic secrets and verify one completion event after repeated setup.
`test_import_and_discovery_do_not_configure_owned_logging` checks the owned setup
is absent during import/discovery and existing embedding handlers remain intact.

## 6. Threat model

Evidence abbreviations refer to the test files/names in the control matrix above.
These threats are constrained by the local deployment assumptions, not a claim
that every STRIDE category applies equally to every component.

| ID / category | Asset / boundary | Threat | Mitigation | Evidence | Residual risk |
| --- | --- | --- | --- | --- | --- |
| S1 Spoofing | Caller/audit identity; B1/B8 | Fake client principal, spoofed request ID or identity metadata | No caller identity inferred; UUID invocation ID; bounded IDs; metadata allowlist | `test_mcp_audit.py::test_stdio_audit_has_no_authenticated_caller_identity`; `test_audit.py::test_request_ids_rejected` | IDs are caller-controlled correlation; no authenticated principal |
| S2 Spoofing | Runtime credentials; B2/B3/B5/B9 | Wrong DB user or Kubernetes identity | Required runtime DB fields, no admin fallback; selected static kubeconfig; separate live identity verifier | Admin-fallback tests; Kubernetes verifier identity mismatch test | Runtime loaders do not prove effective identity/privileges |
| T1 Tampering | Database data; B2/B5 | Write-capable role or unsafe SQL | Restricted provisioning attributes/grants; single SELECT screening; read-only transaction | Slice 1 tests | Existing grants/memberships/ownership, privileged functions or backend extensions require review; parser is not SQL authorization |
| T2 Tampering | Kubernetes resources/policy; B3/B5 | Mutation, wildcard grants, Secret access | Exact narrow RBAC policy; verifier negative permission cases | Slice 2 tests | Extra bindings may expand authority; administrator/control plane trusted |
| T3 Tampering | Docker daemon; B4 | Mutation through tool or identifier injection | Fixed observation tool/SDK calls; bare identifiers | Slice 3 tests; `test_docker.py::test_invalid_identifiers_never_reach_sdk_lookup` | Daemon authority unrestricted after process compromise |
| T4 Tampering | Configuration/result; B6/B7/B9 | Alter endpoint/authentication or manipulate result before redaction | Local validation, snapshot, deterministic diagnostics, centralized result policy | Configuration snapshot and raw-event/redacted-evidence tests | Trusted config controls targets; backend evidence can be false; diagnostics are not provenance validation |
| R1 Repudiation | Audit records; B8 | Missing calls, false-success records, forged metadata | Outermost completion attempt; inspect SDK error result; static schema/categories; handler setup on both starts | Slice 7 and Slice 9 startup tests | No authenticated attribution; crash/setup/sink failure can lose events; success means tool completion, not healthy backend or delivered response |
| R2 Repudiation | Audit integrity; B8 | Audit injection/tampering | Identifier normalization, JSON encoding, value redaction; no observations/exception text | `test_audit.py::test_malicious_metadata_one_line`, `test_json_redacts_values_before_encoding` | Local writer/collector trusted; no durable or tamper-evident store |
| I1 Information disclosure | DB rows/logs/events/evidence; B6/B7 | Nested credentials, event messages or diagnostic evidence reach caller | Credential keys, recognizable forms and registered values redacted after conversion | Slice 4 wire and diagnostic tests | Arbitrary business data or unrecognized secrets can legitimately pass |
| I2 Information disclosure | Credentials/errors/stderr; B7/B8/B9 | Exception strings, repr, logging arguments or traceback chains leak secrets | Constant config errors, safe settings repr; runtime handlers redact formatted traceback; handler failures dropped | `test_configuration.py`; `test_mcp_redaction.py::test_server_stderr_sanitizes_framework_traceback`; Slice 9 failure test | Direct writes, unknown secrets, pre-setup/import failures and independent handlers are outside complete protection |
| I3 Information disclosure | Docker metadata/topology; B4/B7 | Raw inspect exposes env, commands, mounts or health output | Explicit curated projection | Sensitive inspection-fields test | Names/images/IPs/ports remain visible |
| I4 Information disclosure | Audit metadata; B8 | Secrets or sensitive names in IDs/resources | Bounded known fields, no arbitrary metadata, redacted values | Slice 7 schema/injection/identifier-secret tests | Valid names/IDs can contain unrecognized sensitive information |
| I5 Information disclosure | Generated token/admin material; B5 | Kubeconfig permissions or provisioning stderr expose credentials | Private directory/file modes; atomic token-only output; captured kubectl stderr; constant failures | `test_kubernetes_provisioning.py::test_failure_is_secret_safe_and_preserves_old_credentials`, private-output and atomic-write tests; `test_postgres_provisioning.py::test_sql_failure_rolls_back_without_exposing_error` | Trusted filesystem/CLI/dependencies; inherited environment may expose admin credentials to runtime |
| D1 Denial of service | Process/backend availability; B2/B3/B4/B6 | Unbounded rows, huge values/logs/listings/evidence | Streaming and budgets; list cap; log tail/non-following mode; diagnostic counts | Slice 6 boundary tests | One DB row, SDK response or Kubernetes page may be buffered first; logical size is not wire size |
| D2 Denial of service | Kubernetes/API availability; B3 | Repeated or endlessly changing pagination tokens | Token progression checks, page/item bounds | `test_kubernetes.py::test_repeated_token_fails_and_closes`, endless-empty-page test | Many bounded calls still take time; no total deadline |
| D3 Denial of service | Backend/process availability; B2/B3/B4/B8 | Slow calls, repeated observations, concurrency or blocking stderr | Native timeouts; Kubernetes retries disabled; cleanup attempted | Timeout/cleanup tests; `test_audit.py::test_fail_open` | Native timeouts are not total deadlines; aggregate load unbounded; stderr blocking is not an exception |
| E1 Elevation of privilege | Backend authority; B2/B3/B5/B9 | Admin fallback, wildcard/write permissions or token misuse | No DB fallback; restricted source policy; short-lived token request; static kubeconfig | Slices 1/2/5 tests | Runtime does not attest privileges; stolen token allows its granted observations; actual token lifetime controlled by API server |
| E2 Elevation of privilege | Docker/local authority; B4/B10 | Compromised process uses daemon socket directly | Narrow application surface reduces exposed operations | Slice 3 tests | No containment after arbitrary code execution |
| E3 Elevation of privilege | Shared service identity; B1/B9 | Remote exposure gives callers shared backend authority; unsafe config selects broader credentials | Supported module path is stdio; local config owner trusted | Slice 8 startup and configuration tests | Remote SDK hosting possible; remote auth/access isolation deferred |

No additional mocked test can prove effective live DB grants or Kubernetes RBAC.
The current Docker surface and exact SDK call tests already cover the application
boundary; duplicating them would not reduce daemon authority.

## 7. Security invariants

### GUARANTEE

- Supported module startup explicitly selects stdio; arbitrary transport
  environment variables do not change that invocation.
- Import/discovery does not activate infrastructure or call owned logging setup.
- Both supported stdio invocation paths install safe stderr and audit handlers
  at runtime; repeated setup does not accumulate handlers/events.
- Runtime PostgreSQL config never falls back to admin config; it rejects equality
  with the configured admin username when that field is present.
- Provisioning policy creates/configures a restricted PostgreSQL reader; this is
  a policy guarantee, not complete revocation of existing authority.
- Provisioned Kubernetes RBAC excludes mutation and Secret access.
- OpsPilot exposes no Docker mutation MCP tool.
- Registered tool publication passes centralized post-conversion redaction.
- Supported configuration validation occurs before backend activation, including
  both settings in cross-system diagnosis. This does not mean all semantic input
  validation precedes activation: Docker identifier checks occur after client
  construction/version negotiation, before container lookup.
- Observations use bounded retrieval/retained-payload policies.
- Requests reaching audit middleware with method `tools/call` and a non-null
  request ID receive one best-effort completion attempt during normal unwinding;
  notifications are excluded. This is not a guarantee of record delivery.
- Audit records exclude SQL, observations and exception text.
- Request IDs are correlation metadata, not caller identity. Stdio provides no
  authenticated MCP caller identity.

### ASSUMPTION

- Local launcher/OS account is trusted.
- Intended runtime credentials are supplied.
- Backend administrators have not granted additional privileges.
- Installed dependencies/SDKs are trusted.
- Local credential files/socket permissions and filesystem ancestors are protected.
- Backend endpoints, administrators and permission enforcement are trustworthy.

### LIMITATION

- PostgreSQL runtime validation does not prove effective privileges.
- Kubernetes runtime validation does not prove effective RBAC.
- Docker daemon access is not daemon-enforced read-only.
- Redaction cannot identify arbitrary sensitive information.
- Limits are not hard memory or total wall-clock ceilings; SDK buffering can
  occur before application checks. SQL functions can affect session settings.
- Audit is best-effort, not durable/tamper-evident; stderr can block.
- Not every SDK/protocol error passes OpsPilot redaction.
- Health results can be false without a tool execution error; audit success does
  not assert infrastructure health.

### DEFERRED

- Remote authentication and authorization; TLS/network listener policy.
- Per-user/per-resource access control and shared-identity isolation.
- Docker allowlisted proxy/isolation.
- Rate/concurrency and aggregate-observation limits.
- Hard wire-size/memory limits and total-operation deadlines.
- Durable audit storage and stronger delivery/attribution guarantees.
- Dependency provenance policy.

## 8. Docker authority

OpsPilot's Docker MCP surface is observational, but unrestricted Docker daemon
access is not a daemon-enforced read-only security boundary.

**LIMITATION:** there is no read-only socket claim. If arbitrary Python code runs
inside the process, it can use the configured Docker authority independently of
the MCP tools, potentially mutating containers or gaining host-level authority.
Curated projections also do not make the daemon response harmless before SDK
buffering/projection. Remote Docker endpoints are configurable; OpsPilot does not
establish that exposing or using such endpoints is safe.

## 9. Deployment verification

### PostgreSQL

Provisioning SQL/policy is tested offline. Verify effective live role attributes,
grants, memberships, ownership, PUBLIC privileges, executable/SECURITY DEFINER
functions and applicable RLS behavior in the target deployment. Re-provisioning
does not revoke every pre-existing privilege or membership. Default table grants
cover the configured provisioning owner's future tables, not every possible
object creator. Runtime validation does not attest any of this.

### Kubernetes

The RBAC manifest and token-only kubeconfig policy are tested offline. Run the
existing `scripts/verify-kubernetes-reader.py` with the generated kubeconfig to
check authenticated identity and effective positive/negative permissions. The
verifier uses identity/access reviews and observational read paths; it must be
invoked separately. Additional bindings can broaden authority. The API server
controls actual token lifetime, regardless of the requested one hour.

### Docker

Application method surface and projections are tested. OpsPilot does not restrict
daemon authorization. Verify intended daemon endpoint, socket ownership and any
external authorization/isolation policy independently.

## 10. In-scope / out-of-scope decisions

| Actor/input | Scope | Reason |
| --- | --- | --- |
| Malicious local MCP requests | Partially in scope | Validate input, restrict tools, redact publication; no authenticated identity or aggregate-load isolation |
| Compromised trusted MCP client | Partially in scope | Same tool constraints, but can exercise all observations granted to runtime identity |
| Malicious database content | In scope as data | Nested redaction and budgets; no semantic secrecy or downstream instruction-safety guarantee |
| Malicious Kubernetes logs/events | In scope as data | Bounds, projections, deterministic analysis, redaction |
| Malicious Docker logs | In scope as data | Tail/byte policy and publication redaction |
| Malformed/oversized backend responses | Partially in scope | Validate/project/bound retained results; SDK allocation/parsing and backend truthfulness remain trusted |
| Compromised local root/OS | Out of containment scope | Can inspect/alter process, credentials and sockets |
| Malicious Python code already in process | Out of containment scope | Can bypass wrappers and use credentials/daemon directly |
| Compromised dependency or MCP SDK | Out of containment scope | Runs with process authority and owns transport/dispatch behavior |
| Full supply-chain compromise | Out of containment scope | Runtime middleware cannot establish dependency provenance |
| Direct remote MCP callers | Deferred deployment model | Requires explicit auth, transport, authorization and shared-identity decisions |
| Remote attacker influencing backend content | Partially in scope | Treated as malicious observed data, not a supported inbound remote MCP connection |

## 11. Residual risks

- **Low:** cleanup failure may mask the original operation failure, affecting
  diagnostics/audit classification. Cleanup exception precedence is not fixed in
  Slice 9; no demonstrated redaction bypass follows from it.
- Existing PostgreSQL grants/memberships/ownership and Kubernetes extra bindings
  may broaden runtime authority beyond the source-controlled policy.
- A shared environment/dotenv can expose provisioning credentials to the runtime
  process even though runtime connections never substitute admin fields.
- Docker endpoints may represent powerful daemon authority.
- Arbitrary sensitive business data can legitimately be returned by allowed
  SELECT/log operations. Valid resource names can contain sensitive operational
  information; normalization does not make them nonsecret.
- Aggregate load remains unbounded. Multiple sequential diagnostic calls,
  concurrent requests, large SDK buffers and redaction/serialization expansion
  can consume substantial resources despite per-observation policy.
- Audit can be lost on crash, setup failure or sink failure. No completion record
  is guaranteed for abrupt termination or requests rejected before middleware.
- Direct stderr writes, independent handlers, pre-runtime import failures and
  arbitrary unrecognized secrets are outside comprehensive logging protection.
- Provisioning subprocess failures normally use fixed safe messages, but trusted
  dependencies/filesystem cleanup can still fail outside protected paths.

## 12. Review triggers

Review this model when:

- MCP tool surface or transport changes.
- Remote deployment or authentication/authorization is introduced.
- Backend credential mechanisms or Docker access mechanisms change.
- Provisioning grants/RBAC change.
- Logging/audit/redaction ordering changes.
- MCP SDK major/minor behavior changes materially.
- Observation-limit policy changes.

Update claim, mechanism, test evidence and residual risk together. Keep test names
current; do not replace deployment verification with mocked assertions.

## 13. Review metadata

- Project: OpsPilot MCP.
- Current security milestone: M6.
- Reviewed against commit: `528b05d` as the pre-Slice-9 baseline.
- MCP SDK version reviewed: `2.3.0`.
- Slice 9 scope: runtime logging consistency, offline startup security regressions
  and this maintained threat model. No remote transport/auth features added.
