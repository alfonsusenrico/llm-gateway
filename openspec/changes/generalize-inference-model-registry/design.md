# Design

## Context

See `proposal.md` for scope. At baseline `505f6e1`, models are keyed by a closed
provider/model pair, profiles use contract version 1, and the app constructs one
OpenAI client from a process setting. Request validation and `/v1/catalogue` expose
overlapping capability rules. Calls already have atomic admission, an append-only
event ledger, disconnected-stream settlement and encrypted 24-hour results.

The owner approved the broader phased plan on 2026-10-16. This first change
establishes the shared backend contract; it does not add local model execution,
embedding execution or OCR execution. No server access or paid call is required.

## Goals / Non-Goals

Preserve the existing execution-certainty and money invariants while making model
identity, supported fields and provider credentials explicit. Reuse the project
authentication and registry service instead of adding another key system.

Do not introduce CUDA dependencies, arbitrary provider pass-through, provider-key
arguments on inference calls, project content in contracts, or a legacy API layer.

## Decisions

### 1. Keep three execution operations behind a task-aware model registry

Keep `/v1/responses` for LLM execution. Reserve task identifiers `llm`, `embedding`
and `ocr`; the other execution endpoints are delivered in their respective changes.
The registry maps explicit adapter IDs to a task and the fields/capabilities the
adapter can preserve. Phase 1 registers the real OpenAI Responses adapter and
development stub adapter; adding a JSON definition cannot create a new protocol.
Later changes register Ollama, embedding and OCR adapters at this boundary.

Thin routes authenticate and parse. Resolution selects an immutable definition;
admission/settlement own call state and money; adapters translate provider I/O.
Adapters receive an execution context rather than reading arbitrary project data
or global provider credentials. Task-specific request/result types stay separate.

Rejected: one universal inference payload, which loses operation-specific types;
and a provider `Literal` enum in each layer, which duplicates integration work.

### 2. Store one strict model definition per fixed alias

Package definitions under `catalogue/models/<alias>.json`. The public alias grammar
is `^[a-z][a-z0-9._-]{1,159}$`; aliases are global across tasks and need not match
filenames, though packaged files use that convention. The loader scans its own
configured directory and never opens a path derived from a request.

The typed definition includes schema version, alias, task, provider/upstream model,
adapter ID, logical deployment reference, execution kind, billing basis,
capabilities, supported parameter rules/defaults, input/output limits,
development-only status and documentation references. A canonical execution
subdocument receives a SHA-256 digest. It includes the task, routing reference,
upstream/configuration identity and effective behavior, including defaults/limits.
Documentation and effective-dated prices are excluded from this digest.

Keep the existing price schedule and its provider/model keys; do not rewrite past
prices. Paid definitions require current standard pricing. Local/stub definitions
declare zero provider fee explicitly. Unknown paid prices remain unavailable.

Add `model_alias_bindings(alias, task, definition_digest, registered_at)` with an
append-only database guard. `llm-gateway-admin models sync` atomically registers
new aliases or verifies existing digests. Removing a definition from the active
files hides it without deleting its binding, so its alias cannot be recycled.
The migration Compose step runs schema migrations then model synchronization;
neither happens in API startup. Requests refuse unsynchronized definitions.

Initially convert the two existing model definitions into aliases
`openai-gpt-6-luna` and `stub-echo`. Do not add speculative live local definitions.
An alias fixes the gateway's mapping; upstream revisions are pinned where the
provider supports that, without claiming control over provider-side model changes.

Rejected: a filename as a public contract, mutable convenience aliases, and a
git-only immutability check that forgets aliases after retirement.

### 3. Generate and execute the same effective request schema

The operation schema supplies structural fields and trust-boundary bounds. Each
model's typed rules generate its effective direct-call JSON Schema, including
`additionalProperties: false`, allowed fields, enums, limits and conditional rules.
Validate the expanded request with that generated schema using a cached
Draft-2020-12 validator; add `jsonschema` as a locked project-local dependency.
Check supplied-field presence before materializing defaults, so an unsupported
field cannot disappear into a serializer default or be silently ignored.

Profiles provide only fields designated as eligible parameter defaults; initially
this preserves reasoning effort, temperature and maximum output tokens. They never
contain input, instructions, tool implementations or secrets. Resolve the profile,
apply explicit overrides, materialize supported defaults and validate the direct
alias form. Cross-item invariants such as matching tool outputs remain typed
service validation. Model details describe these additional semantic constraints.

`GET /v1/models` returns summaries sorted by alias; optional task filtering accepts
the three task identifiers. `GET /v1/models/{alias}` adds the effective direct-call
`request_schema`. Both require existing project authentication and hide development
models from production keys. Detail returns `404 model_not_found` for hidden or
unknown aliases. Availability describes local configuration only, without probing
providers or testing credentials; a missing credential does not hide a supported
paid model. Never expose private endpoints, secret material or internal file paths.

The base OpenAPI operation schema documents alias/profile selection and shared
fields. Effective model schemas express the narrower rules. Request validation
errors contain field paths and fixed codes, never input values. Generate request
and typed response documentation from the actual schemas, including streamed
content types, instead of maintaining a second handwritten API specification.

Rejected: capability flags documented independently of validators, arbitrary
provider kwargs, and putting project prompts in the gateway registry.

### 4. Reuse project keys and store provider credentials separately

Gateway bearer authentication is unchanged. Create a registry-owned
`project_provider_credentials` slot per `(project_id, provider)`, shared by that
project's two environments. Environment-specific provider slots are not introduced.
Keep a UUID revision, current encrypted value or removed status, update metadata,
and a content-free append-only credential-event trail. Every set/replace/remove
receives a fresh revision. Retain no historical secret ciphertext on replacement.

Use the already installed cryptography package's authenticated Fernet encryption
with a dedicated `PROVIDER_CREDENTIAL_ENCRYPTION_KEY`, distinct from result keys
and `PROJECT_KEY_PEPPER`. The encrypted envelope includes project ID, provider and
revision; verify those against the row on decrypt to detect ciphertext swapping.
Typed settings validate key format and report variable names only. The local
setup helper generates the setting without displaying it when the operator runs
setup. No existing `.env` or real provider credentials are changed by this proposal.

Extend the existing CLI/menu with `project credential set`, `remove` and `list`.
`set` takes project/provider identifiers plus a non-echoing terminal prompt or
explicit `--stdin`; never a key argument. Credential-command argument errors use
fixed text rather than echoing rejected argv values. Reject non-TTY implicit fallback and
bound input to 16 KiB with no header-control characters. Print safe status only.
Registry methods enforce project existence and known credential-requiring provider.
Contract import/export/history and all model responses exclude secrets/ciphertext.
Do not perform a provider request during registration.

Credential master-key replacement is an owner-directed operation: pause paid calls,
preserve the prior configuration for recovery, configure the new master key and
re-register each provider credential through the protected registry command before
resuming paid calls. Never change the project-key pepper or result key as part of
that operation. Loss of the credential master key requires re-registration; there
is no decryption fallback. Keyring/reencryption tooling and actual master-key
replacement are outside this first change.

Rejected: plaintext database values, secrets in immutable contract JSON, per-call
provider-key headers, one shared OpenAI credential, and trying to reuse project
access-key hashes as decryptable credentials.

### 5. Capture request-scoped execution credentials without cross-project state

During new paid admission, read/decrypt the current project's provider credential
and capture its revision consistently with the admitted snapshot. Credential writes
and that capture use the same short project-row lock, with no network calls inside
the transaction. Profile-based attempts verify the current contract version under
that lock and re-resolve if a cached contract became stale. Hold the secret only
in a non-serializable, redacted execution
context through that attempt. A concurrent replacement affects later admissions,
not already captured execution contexts. Removal erases the slot ciphertext.

The OpenAI adapter constructs a call-scoped authenticated SDK client, with retries
disabled and deterministic cleanup after direct execution or stream-producer
settlement. Never mutate an app-global client's API key. Always send `store: false`
and the existing client request identifier. Adapter exceptions stay in the bounded
failure table; no automatic request is made to validate a newly stored credential.

Missing credentials fail before admission. Unreadable envelopes fail only the
affected paid calls and raise a deduplicated project/provider alert. A provider's
authentication refusal retains `provider_account_blocked`, zero charged spend and
a project-scoped alert. Existing alert keys must gain the project identifier so a
bad key in one project does not suppress another project's incident.

Rejected: long-lived mutable provider clients and provider-side retries that can
execute outside the gateway's call state machine.

### 6. Make model/profile and credential changes explicit in idempotency

New call fingerprints are schema-versioned and include task plus the canonical
client request, excluding transport streaming mode. Add nullable legacy-safe call
metadata: model alias, execution-definition digest, effective-settings digest,
credential revision, billing kind and task. Existing rows retain original hashes,
state, charges and result ciphertext; mark them with the legacy request-schema
version, never invent their historical model/credential bindings.

For the current request schema, check project authentication and the original
fingerprint before live model/profile/credential resolution on an existing terminal
call. Replay its stored outcome or return result expiry; do not contact providers.
Unknown executions remain terminal even after configuration/credential changes.

For confirmed-refusal retries, re-resolve and compare effective settings/definition
and credential revision to the prior attempt. Return `409 call_configuration_changed`
or `409 credential_changed` before readmission if either changed. The caller must
intentionally start a new call ID to request execution under a new binding.
Concurrent edits cannot make the recorded revision differ from the executed one.

Use one project/environment call-ID namespace across tasks. A different task or
request conflicts instead of replaying another operation. Preserve the current
state transitions, disconnected-consumer behavior and one-charge settlement.
Return `model_alias` and `definition_digest` alongside the current provider/model
result fields. Continuations carry the alias/digest as well as upstream identity;
cross-definition continuation is refused even for identical upstream model names.

Rejected: re-resolving finished calls against the newest settings, storing raw
credentials in fingerprints, and replaying an uncertain request with another key.

### 7. Separate explicit zero fees from paid-budget exhaustion

All new attempts still require a grant and pass shared project rate/concurrency
checks. Paid attempts retain the global USD 5 monthly cap, project grants,
per-call ceiling, conservative reservation and outcome-based settlement. This
counts gateway-managed calls across project-owned keys, not external account use.

For explicit zero-fee execution, reserve/settle zero and skip monetary ceilings,
budgets and threshold alerts; never reinterpret unknown paid pricing as free.
Use the existing development stub to verify this path without installing local
models. Actual local models use it when their adapter change ships.

Rejected: removing paid limits because keys are project-owned, and refusing local
work because a paid budget is exhausted.

### 8. Replace the selection contract and client together

Keep the LLM operation's existing body fields and exactly-one selector rule, but
make `model` a string alias. Keep profiles as an optional explicit selector.
Contract schema version 2 represents each profile as `{model, parameters}`.
Remove `/v1/catalogue`, provider/model request objects and `OPENAI_API_KEY` runtime
execution configuration. There is no automatic fallback to old keys or old requests.

The replacement client exposes sync/async `models.list(task=...)`, `models.get(alias)`
and alias-based `responses.create`/`responses.stream`. It sends explicit settings,
not a full set of model-specific defaults. Preserve typed stream/tool results and
retry certainty. Add every new problem code to the shared gateway/client guard:

| Code | Status | Automatic retry |
| --- | --- | --- |
| `model_not_found` (discovery details) | 404 | No |
| `model_task_mismatch` | 422 | No |
| `model_registry_unavailable` | 503 | No; operator configuration required |
| `provider_credentials_missing` | 503 | No |
| `provider_credentials_unreadable` | 503 | No |
| `contract_upgrade_required` | 503 | No |
| `credential_changed` | 409 | No |
| `call_configuration_changed` | 409 | No |

Retain existing model/input/settings and provider failure codes where their
meaning is unchanged. Keep the project/package names. Release matching gateway and
client artifacts together; final README/examples are completed after backend maturity.

Rejected: maintaining parallel old/new APIs in this local development stage, and
removing useful explicit profiles merely to simplify alias requests.

## Migration and recovery

1. Stop consumers and drain/settle in-flight requests before switching the API.
   Owner runs the normal setup path for the new encryption setting, without
   changing the pepper or result key. Back up local data through the approved
   procedure before applying migrations; this proposal performs none of those actions.
2. Add forward-only migrations after existing `0005`: alias bindings, credential
   slots/events and nullable/versioned call metadata. No applied migration edits,
   deletion/repricing of old rows, or mandatory destructive schema contractions.
3. Run model synchronization. Add `project upgrade-contract <id>` and `--all`,
   implemented in the registry under row locks. It creates new version-2 documents
   by unambiguous provider/model-to-alias mapping and preserves original profile
   parameters, requests/status/weights/keys and ledger. Repetition is a no-op.
4. Preserve historical version-1 documents and readers. A still-current version-1
   project receives `contract_upgrade_required`, not a malformed-contract alert.
   Unknown alias mappings leave that project unchanged and require explicit owner
   correction; independent project upgrades continue with an aggregate failure exit.
5. Owner registers each project's provider credential with the protected CLI/menu.
   Never read a legacy `.env` provider key into the new registry implicitly.
6. Start the new backend/client. Old wire requests are rejected. Legacy call IDs
   and outcomes stay recorded but are not translated into schema-2 requests or
   executed again; retained old ciphertext expires under its original schedule.
   No personal-agent edits are performed by this change.

Before contract upgrades, the additive schema can coexist with the old binary.
After version-2 activation, do not roll an old binary over those active contracts.
Operational recovery is to keep the version-aware binary, disable affected paid
execution through the registry, and fix forward. A code downgrade requires a
separately reviewed version-aware bridge; never restore an old database over new
ledger activity or rewrite contract history to simulate rollback.

## Verification approach

Use real disposable PostgreSQL for migrations, registry, locks and ledger tests;
in-process API/client tests and synthetic HTTP/SDK provider fakes for execution.
No server hardware, model selection, real documents or paid test calls are needed.

Test schema/discovery agreement, fixed aliases after retirement, concurrent
project credentials and rotations, ciphertext owner binding, protected management,
unchanged terminal replay, changed-retry refusals, preservation of legacy money
and keys, zero-fee admission after paid exhaustion, streaming and tool continuation.
Validate generated OpenAPI and all problem-code mappings. Run `./scripts/check`
and both normal/strict OpenSpec validation before implementation delivery.

## Risks / Trade-offs

- A gateway database plus its credential master key can decrypt registered secrets:
  keep the key outside the database and apply existing secrets/privacy controls.
- Per-call SDK clients add lifecycle work: cleanup belongs to the execution task,
  including detached streaming, and is covered by concurrency/disconnect tests.
- Fixed aliases create additional definitions when behavior changes: preserve
  clarity of stored embedding identity rather than silently repointing old names.
- A breaking local upgrade pauses consumers: migration is explicit, and preserved
  ledger/idempotency data prevents the upgrade from authorizing duplicate execution.
- Schema validation alone cannot describe all tool/continuation invariants: expose
  semantic constraints and keep focused service validation alongside the schema.

## References

- [Fernet authenticated encryption](https://cryptography.io/en/stable/fernet/),
  reviewed 2026-10-16 for credential envelopes and key handling.
- [OpenAI error codes](https://developers.openai.com/api/docs/guides/error-codes),
  reviewed 2026-10-16 for provider authentication failure classification.
- Existing OpenAI adapter/provider review and current approved call/ledger specs.
  No pricing changes or new paid model are introduced in this change.

No unresolved product-policy questions remain. Implementation mechanics above are
proposed for approval with this change; later inference endpoints have their own specs.
