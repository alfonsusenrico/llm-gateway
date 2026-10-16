# Verification: generalize-inference-model-registry

- Date: 2026-10-16; local development only.
- Branch: `feat/inference-model-registry`, based on `505f6e1`.
- Approval: owner approved the phased plan and this concrete foundation change.
- Tested state: implementation working tree, gateway/client version `0.4.0`.
  Implementation revision: `f89e7ba`. PR: https://github.com/alfonsusenrico/llm-gateway/pull/15.
- No paid provider calls, server access, deployment, consumer edits, real credential
  registration, or application-database migration was performed.

## Checks

| Check | Method | Result |
| --- | --- | --- |
| Complete gate | `./scripts/check` | Passed: lock, formatting, lint, Pyright (0 errors), 716 tests with DB required, Compose config, 10 OpenSpec items |
| Strict specifications | `openspec validate --all --strict --no-interactive` | 10 passed, 0 failed |
| Image build | `docker build --build-arg APP_REVISION=505f6e1-inference-working-tree -t llm-gateway:inference-verification .` | Passed; packaged gateway 0.4.0, non-root user |
| Actual artifact | Uvicorn process from the image, real HTTP calls, isolated disposable PostgreSQL 16 | Passed: migrations/repetition, model synchronization/repetition, readiness, discovery/schema, direct/streamed stub calls, replay, missing paid credentials, ledger and OpenAPI |
| Packaged source identity | SHA-256 of sorted relative package paths and bytes (`.py`, `.json`, `.sql`) on host and in image | Equal: `fd6a40eb6f725aa1dc22e535c5604b5ca8705a6a912b86f8f21fff652527a6f8` |
| Image identity | Local verification image | `sha256:b611ba47e327f9230133af53e97d44a865fa5714c99a0c45adfc8664554dc7a6`; runtime UID 10001 |
| Diff/privacy | Changed and added files, migration history, secret/input redaction, ownership and money paths | Reviewed; no real settings or data included |
| Framework project check | `af project check .` | Not run: `af` executable unavailable; no tooling installed. Project instructions are 7,768 bytes |

The image check used task-created containers and generated synthetic settings,
without mounting `.env`, application volumes or personal files. Its temporary
PostgreSQL service has no published host port. The optional local smoke driver is
under ignored `research/evidence/generalize-inference-model-registry/`; no private
infrastructure is needed to reproduce the tracked regression tests.

## Acceptance evidence

| Area | Tracked evidence |
| --- | --- |
| Definitions and pricing | `tests/test_catalogue.py`: packaged definitions, duplicates, unsupported fields, defaults, profile settings and dated pricing. `tests/test_inference_registry.py`: adapter identity validation, immutable alias bindings, retirement/reuse, concurrent and atomic sync. Existing price history is unchanged |
| Effective schemas | `tests/test_inference_registry.py`: unsupported explicit presence, reasoning/temperature conditions, safe validator errors and independent schema copies. `tests/test_llm_request.py`: model/default/profile/input checks and presence-sensitive, stream-independent fingerprints |
| Credential isolation | `tests/test_provider_credentials.py`: ciphertext-only slots, bound project/provider/revision envelopes, wrong key/corruption/substitution refusal, redacted metadata, revision replacement/removal, audit rollback/immutability and concurrent rotation |
| Protected management | `tests/test_admin_projects.py`, `tests/test_admin_menu.py`, `tests/test_provider_credentials.py`: hidden input, no echo fallback, explicit stdin, rejected value arguments, CLI/menu set/list/remove, export/history exclusion and model sync |
| Setup/key separation | `tests/test_setup_local.py`, `tests/test_settings.py`: separate generated encryption keys, existing values preserved, idempotence, mode 0600, symlink refusal and redacted invalid configuration. Only temporary settings files were created |
| Versioned contracts | `tests/test_inference_registry.py`: preserved keys/requests/weights/grants/history, concurrent/no-op upgrades, atomic unmapped refusal, mixed all-project upgrade outcomes, historical readers and authenticated upgrade-required behavior without quarantine alerts |
| Migration preservation | `tests/test_migrate.py`: fresh/repeated application plus old contract, key, fingerprint, state, money, event and encrypted-result rows preserved byte-for-byte across 0006-0008; new legacy metadata remains absent |
| Execution binding | `tests/test_inference_calls.py`: simultaneous projects use different registered accounts; persisted revisions/digests match execution; profile/credential edits cannot split admission. A waiting admission reads the version committed by the preceding profile writer |
| Retry/replay | `tests/test_inference_calls.py`: terminal replay survives model/profile retirement and credential removal; confirmed-refusal retries reject rotated/removed credentials or changed profile settings. Existing response/stream tests cover unknown execution, 24-hour expiry, lost response, disconnects and one-charge replay |
| Paid/free accounting | `tests/test_calls.py`: existing exact money, paid cap/concurrency races, reservations, settlement and append-only events. `tests/test_inference_calls.py`: zero paid grant/global cap with zero reservation/charge, no paid price snapshot or monetary alert, preserved rate/concurrency enforcement |
| OpenAI lifecycle | Existing adapter/stream tests plus `tests/test_inference_calls.py`: captured synthetic account, `store: false`, SDK retries off, separate direct/stream client cleanup. Streaming producers explicitly close provider generators before settlement; disconnect tests retain execution ownership |
| Discovery/API | `tests/test_auth_api.py`, `tests/test_inference_calls.py`: authentication, task filtering, dev-only hiding, unknown aliases, missing credentials, destination/secret exclusion, executable published schema, typed result and problem OpenAPI, removed legacy route and invalid requests rejected before admission |
| Tools/client | `tests/test_function_tools.py`, `tests/test_client_contract.py`, `clients/python/tests/`: identity-bound tool continuations, same-upstream cross-alias rejection, direct/stream parity, sync/async discovery, alias serialization, schema inspection, stub completion/replay, configuration failures never retried and exhaustive gateway-code mapping |

Earlier transitional failures came from old fixtures and request shapes; the final
gate above supersedes those runs. The concurrent-writer regression checks that a
request is actually blocked on the writer before releasing it. Package checks and
real HTTP artifact checks supplement the in-process API/client tests.

## Definition and wire contract

Each `src/llm_gateway/catalogue/models/*.json` defines one alias. The validated
fields include schema version, alias, task, provider/upstream model, adapter,
logical deployment, execution type, billing type, input/output limits, supported
reasoning efforts/default, temperature policy, service tiers and capability flags.
Adapter identity/capabilities must be implemented and registered in code; JSON
cannot enable an arbitrary provider. Existing entries are `openai-gpt-6-luna` and
`stub-echo`; local adapters and embedding/OCR definitions come in later changes.

The immutable digest covers the normalized definition except documentation links.
All execution-affecting replacements require new aliases. `models sync` reserves
aliases permanently in PostgreSQL, including after a definition is retired; a
conflict rolls back the entire synchronization. API startup performs no schema or
alias writes. The normal Compose migration step synchronizes after SQL migrations.

Discovery is `GET /v1/models?task=llm` and `GET /v1/models/{alias}`. Details include
the effective direct-call JSON Schema. The same schema validates merged settings;
additional semantic checks enforce tool names/choice, continuation order/identity
and typed content rules. Availability reports local registry/adapter/credential
configuration, without probing provider authentication or revealing destinations.
Development Swagger remains at `/docs`.

Breaking changes, released together as gateway/client `0.4.0`:

- Call `model: "openai-gpt-6-luna"` or select a profile; provider/model request
  objects and request credentials are rejected.
- Contract schema 2 profiles use
  `{ "model": "openai-gpt-6-luna", "parameters": { "reasoning_effort": "low", "max_output_tokens": 1200 } }`.
  Registry document version numbers remain separate from the contract schema version.
- Replace `client.catalogue()` with sync/async `client.models.list()` or
  `client.models.get(alias)`; `/v1/catalogue` is removed.
- Results and opaque tool continuations carry `model_alias` and
  `definition_digest`. Preserve the continuation unchanged between matching calls.
- Paid credentials are registered once per project/provider. Runtime
  `OPENAI_API_KEY` execution fallback is removed. Projects continue using their
  existing gateway access keys and grants.
- Configuration failures (including missing credentials/upgrade/registry binding)
  are not automatically retried. A changed credential/configuration requires a
  fresh call ID after a confirmed refusal. Unknown executions are never run again.

Completed result bodies remain encrypted for the default 24 hours. Replay stores
outputs needed for transport recovery, not prompts/images or provider credentials.
Persistent content-free ledger fingerprints and certainty states survive expiry.
Explicitly zero-fee execution skips monetary ceilings/caps but obeys grants, rate
and concurrency. Unknown paid pricing is still refused or conservatively charged.

## Owner-run migration and recovery (not executed)

1. Pause consumers and drain existing calls. Back up local data using the approved
   procedure before applying migrations. This change has not touched the running
   application database or real `.env`.
2. Run `./scripts/setup-local` to add only the missing dedicated credential key.
   Preserve `PROJECT_KEY_PEPPER` and `RESULT_ENCRYPTION_KEY`. Protect a recoverable
   copy of the credential master key outside the database; setup never imports a
   legacy provider key.
3. Build the reviewed image through the normal Compose path. Run its `migrate`
   service: schema migrations 0006-0008 then `llm-gateway-admin models sync`.
   The changes are additive and do not rewrite historical calls, money or keys.
4. Run `llm-gateway-admin project upgrade-contract sample_project`, or
   `llm-gateway-admin project upgrade-contract --all`, in the reviewed environment.
   Each upgrade appends one version under a project lock; repetition is a no-op.
   Ambiguous/unmapped profiles refuse that project atomically while independent
   projects continue. Historical v1 documents stay readable; active v1 requests
   receive `contract_upgrade_required` without key revocation.
5. Register credentials using
   `llm-gateway-admin project credential set sample_project --provider openai`.
   This uses protected terminal input. For a protected stdin source, add `--stdin`;
   never put a secret in argv, a contract, a command example or a request body.
   `project credential list sample_project` returns safe metadata only;
   `project credential remove sample_project --provider openai` erases the slot's
   ciphertext and writes a new audit revision.
6. Coordinate matching client upgrades before starting consumers with alias calls.
   No personal-agent migration is included. Final consumer onboarding/README work
   follows backend maturity, as requested.

Back up the master key separately from ciphertext. If it is unavailable, restore
that exact key securely or explicitly re-register affected credentials under a
properly provisioned key; there is no plaintext recovery/export or silent fallback.
Removal/rotation does not rewrite completed outcomes or old audit identities.

Before contract upgrades the additive schema supports the old binary. After v2
activation, keep the version-aware binary and fix forward; an old binary cannot
read current v2 contracts. Never rewrite contract history or restore an old database
over newer ledger activity to simulate rollback. Dedicated master-key rotation/
reencryption tooling is outside this change.

## Not verified / deferred

- Paid account authentication and actual provider billing: no live calls authorized.
- Server provisioning, local model benchmarks, Ollama integration, embeddings,
  PNG/JPEG OCR and consumer migration: separate changes/coordination.
- Production deployment, tags, hosted CI and final consumer documentation: deferred
  under the owner's existing instructions. No deployment acceptance is implied.

## Delivery

Implementation `f89e7ba` is pushed on `feat/inference-model-registry` in
[PR #15](https://github.com/alfonsusenrico/llm-gateway/pull/15). All 23 tasks are
complete. The delivery-record follow-up changes only this evidence and task status;
its full gate passed with 716 tests. Disposable verification containers/network
were removed; the built image is retained locally. The owner reviews and merges;
no merge or deployment has occurred.
