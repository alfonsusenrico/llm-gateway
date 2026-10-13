# Tasks

## 1. Request handling

- [ ] 1.1 Add the request models (strict), size limit, resolution (profile,
  catalogue, settings rules, input types, stub rights), and the canonical
  fingerprint. Verify with unit tests for each refusal code in the
  `llm-calls` request scenarios.
- [ ] 1.2 Add settings (`OPENAI_API_KEY`, `RESULT_ENCRYPTION_KEY`,
  `RESULT_RETENTION_SECONDS`, `REQUEST_MAX_BYTES`,
  `DEFAULT_MAX_OUTPUT_TOKENS`), `.env.example`, compose, and `setup-local`
  generation of the result key. Verify with settings tests.

Expected Result: invalid calls are refused before admission, at no cost.

## 2. Execution

- [ ] 2.1 Add the `openai` and `cryptography` dependencies and the OpenAI
  adapter (`store=false`, `max_retries=0`, `X-Client-Request-Id`, input
  mapping, output extraction, usage, headers). Verify with adapter tests
  against a mocked HTTP transport that assert the outgoing request body and
  headers.
- [ ] 2.2 Implement the failure policy table. Verify with one adapter test
  per row (including connect error versus read timeout, quota codes, `status:
  failed`, an unparseable body) and a test that provider messages never
  reach logs or responses.
- [ ] 2.3 Implement the stub adapter with scenarios. Verify with tests per
  scenario.

Expected Result: every provider outcome maps to one safe, tested category.

## 3. Endpoint, replay, and retention

- [ ] 3.1 Add migration `0005_call_results.sql`, the encrypted result store,
  and worker cleanup. Verify with db tests for encrypt and decrypt, expiry,
  an unreadable key, and cleanup.
- [ ] 3.2 Add `POST /v1/responses`: the flow of admit, execute, settle,
  store, and respond, with allowance headers, shielded settlement, replay,
  and problems; documented in OpenAPI. Verify with API tests using the stub
  for success, JSON, every scenario, replay, conflict, in progress, and
  expiry, plus one call-key log line with no content.
- [ ] 3.3 Document calls, the stub, and retention in `README.md` and
  `AGENTS.md`. Verify that the documented examples run against the local
  stack with a development key.

Expected Result: projects can call models through the gateway safely.

## 4. Live verification (owner-authorised, paid)

- [ ] 4.1 With the owner's `OPENAI_API_KEY`, run on the local stack:
  - one text call and one JSON-schema call on gpt-6-luna;
  - one image call with a small generated image;
  - a resubmission of the text call ID (replay, no new charge);
  - two calls sharing a prompt prefix of more than 1,024 tokens (cached
    tokens appear in the ledger).

  Record the usage, the ledger costs against the price formula, and the
  total spend (expected under USD 0.01) in `verification.md`.
- [ ] 4.2 Run `./scripts/check` and record the counts in `verification.md`.

Expected Result: real calls work and are priced exactly.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
