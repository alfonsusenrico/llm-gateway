# Tasks

Implementation starts after approval of this concrete change. All provider fixtures
are synthetic; no task requires a paid provider call, server access or consumer
repository changes. Focused DB checks require a disposable `TEST_DATABASE_URL` and
`REQUIRE_DB=1`; the complete gate remains `./scripts/check`.

## 1. Model definitions and immutable registration

- [x] 1.1 Replace the closed model-pair catalogue with strict per-alias definitions
  and task-compatible adapter registration; convert existing OpenAI/stub entries
  without editing price history. Verify catalogue load, duplicate/unsupported
  definition and packaged-data tests in `tests/test_catalogue.py` and
  `tests/test_container_build.py`.
- [x] 1.2 Add a forward-only alias-binding migration and atomic `models sync`
  CLI/menu behavior, integrated after the Compose schema step. Verify fresh/repeat
  migrations, identical sync, changed/retired alias rejection, rollback of partial
  sync and no startup migrations with real-DB tests.
- [x] 1.3 Generate effective request schemas and validate resolved payloads against
  them using a locked JSON Schema dependency. Verify allowed/default/conditional
  settings, unknown fields, unsupported adapter capabilities and explicit parameter
  presence in focused schema/request tests; document the definition format in this
  change's verification evidence.

Expected Result: Reviewed definitions are registered immutably and public
supported-field schemas are executable validation contracts.

## 2. Encrypted provider credential registry

- [x] 2.1 Add forward-only credential-slot/event migrations, typed encryption-key
  settings and a registry-owned encrypted store. Verify ciphertext-only database
  storage, owner/provider/revision envelope binding, missing/wrong-key failures,
  fresh revisions and no contract-history mutation with real-DB tests.
- [x] 2.2 Add protected set/replace/remove/list CLI/menu commands. Verify hidden
  input, explicit stdin, rejected value arguments without argv-value echo,
  safe metadata, ciphertext erasure, mutation atomicity and captured-output
  redaction in admin/store tests.
- [x] 2.3 Materialize the dedicated setting through normal setup/Compose settings
  paths without exposing it or importing a legacy OpenAI key. Verify setup/settings
  tests on temporary files, key-purpose separation and no automatic real `.env`
  edits. Record protected provisioning/recovery commands in the verification file.

Expected Result: Projects have isolated, registry-managed provider credentials with
no plaintext readback, export, argv or logging path.

## 3. Versioned contract migration

- [x] 3.1 Implement version-2 alias-backed profiles and strict parameter validation,
  keeping historical version-1 readers. Verify valid profiles, explicit overrides,
  excluded credential/destination fields, unchanged-profile behavior after model
  retirement and contract round trips in contract/registry tests.
- [x] 3.2 Add explicit per-project/all-project upgrade commands through the registry
  service with row locks. Verify preserved project keys/requests/weights/grants/
  history, deterministic legacy mappings, unmapped-profile atomic refusal and
  repeated/concurrent upgrade behavior in real-DB/admin tests.
- [x] 3.3 Return `contract_upgrade_required` for still-current version-1 projects
  without key revocation or malformed-contract alerts. Verify unaffected upgraded
  projects and historical CLI access in authentication/app tests; record the exact
  local upgrade sequence and rollback limits in the verification file.

Expected Result: Active contracts use explicit aliases while historical data and
gateway project access keys remain intact.

## 4. Execution identity, credentials and money

- [x] 4.1 Add nullable/versioned call-identity metadata and task-tagged request
  fingerprints without rewriting old calls or results. Verify migrated legacy
  hashes/states/charges, cross-task conflicts and preserved append-only call events
  with migration/admission/settlement tests.
- [x] 4.2 Refactor resolution/admission to capture effective definition/settings and
  credential revision atomically, and construct a redacted execution context.
  Verify credential/profile edit races, matching execution snapshots, changed-retry
  refusals and absent paid-credential lookup for stub execution with concurrency tests.
- [x] 4.3 Make OpenAI clients call-scoped, remove process-wide provider-key execution,
  and scope provider-auth alerts per project. Verify concurrent-project isolation,
  no shared-key fallback, `store: false`, retries disabled, safe failures and client
  cleanup after direct/streamed/disconnected calls in adapter/streaming tests.
- [x] 4.4 Replay matching terminal calls before live resolution and preserve the
  original result/outcome. Verify retirement/profile edits/credential removal,
  24-hour expiry, unknown execution, one charge and no inference-input persistence
  in real app/result-store tests.
- [x] 4.5 Keep paid admission/settlement invariants and give explicit zero-fee calls
  a monetary bypass with no threshold/pricing alerts. Verify paid cap races, exact
  USD arithmetic, zero paid grant/exhaustion, rate/concurrency enforcement and
  unavailable paid pricing in admission/ledger/budget tests.
- [x] 4.6 Add alias/digest result and continuation identity while preserving tool
  argument and streamed event behavior. Verify same-definition loops, cross-alias
  continuation rejection and direct/streamed result parity in function-tool tests.

Expected Result: Every execution matches its recorded account/model/configuration,
paid limits remain enforced, and finished/unknown calls never execute twice.

## 5. Public discovery and typed API contracts

- [x] 5.1 Add model list/details with task filtering, safe availability metadata,
  development restrictions and exact effective schemas; remove `/v1/catalogue`.
  Verify key authentication, hidden/unknown aliases, absent credentials, internal
  destination redaction and empty future-task lists in API tests.
- [x] 5.2 Replace request model objects with aliases and expose accurate typed
  OpenAPI request/result/problem/stream shapes. Verify invalid models/parameters
  fail before provider/admission and generated schemas match actual payloads in
  request/OpenAPI/response tests; retain development Swagger exploration.

Expected Result: Projects can discover exactly what they can request through a
documented, task-specific API without learning private execution configuration.

## 6. Typed Python client

- [x] 6.1 Implement sync/async `models.list/get`, alias selection and resolved result
  metadata, removing old catalogue/model-object serialization. Verify typed shapes,
  explicit-field preservation, tool helpers and streaming in client tests.
- [x] 6.2 Map every new gateway code and preserve bounded certainty-aware retry
  behavior. Verify non-retryable configuration 503s, changed binding errors,
  transport recovery and the complete problem-code guard in client/app contract tests.
- [x] 6.3 Record matching gateway/client version expectations and concrete breaking
  request/profile changes in the verification file. Verify a synthetic client can
  list a model, inspect its schema and complete/replay a stub call against the app.

Expected Result: A project uses its existing access key and model alias without
supplying provider credentials or implementing its own retry semantics.

## 7. Integration and delivery

- [x] 7.1 Update `AGENTS.md` and OpenSpec context to the approved inference boundary,
  encrypted project/provider credentials and paid-only cap. Keep final consumer
  onboarding deferred. Verify instruction consistency and project checks if the
  framework tool is available, and review the full diff for secrets/unrequested scope.
- [x] 7.2 Run `./scripts/check`, normal and strict OpenSpec validation, and the actual
  local application artifact against synthetic upstreams/disposable PostgreSQL.
  Record exact revision, passed/failed/not-run results, migration preservation,
  model synchronization and redaction evidence in `verification.md`.
- [x] 7.3 Commit on the topic branch only after checks pass; push and open an
  owner-reviewed PR under project policy. Report its URL, branch/revision,
  verification record and breaking changes; update local project state. Never
  merge, deploy, run paid calls or change the consumer repository.

Expected Result: The foundation is reviewable with complete local evidence; later
inference endpoints can integrate without changing these ownership boundaries.

## Workflow follow-up

- Owner reviews/merges; archive using native OpenSpec after review requirements.
- Continue with `add-ollama-llm-provider`, then embedding and OCR proposals under
  the approved phased plan.
- Finish the consumer README/endpoint guide after backend maturity. Server
  integration, actual provider credentials and personal-agent migration remain
  separately coordinated work, not unverified completion claims for this change.
