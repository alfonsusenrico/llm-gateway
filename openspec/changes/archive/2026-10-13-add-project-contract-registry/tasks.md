# Tasks

## 1. Contract model and storage

- [x] 1.1 Add migration `0002_projects.sql` (projects with `weight`,
  `contract_versions` as jsonb, and `project_keys`). Verify with the migrate
  tests.
- [x] 1.2 Implement contract version 1 validation (strict, unknown fields
  rejected, bounds, ceiling not above budget, profile structure). Verify with
  unit tests, one per spec scenario of `project-contracts` that covers
  validation.
- [x] 1.3 Implement the repository and service: register, versioned edits under
  `FOR UPDATE`, history, show by version, enable and disable, export and
  import. Verify with db tests for history, concurrent edits, round trip, and
  import of an unknown project.
- [x] 1.4 Implement the contract cache (10 s) and quarantine of an invalid
  stored contract, with an error alert. Verify with db tests: a disabled
  project takes effect after the cache expires; a corrupted row affects only
  its project and raises one alert.

Expected Result: contracts are validated, versioned, and loaded safely.

## 2. Keys and authentication

- [x] 2.1 Add `PROJECT_KEY_PEPPER`, `AUTH_FAILURE_LIMIT_PER_MINUTE`, and
  `TRUSTED_CLIENT_IP_HEADER` to settings, `.env.example`, and
  `scripts/setup-local` (the pepper is generated). Verify with settings tests
  for a missing or short pepper.
- [x] 2.2 Implement key generation, format parsing, HMAC storage, rotation with
  grace, and revocation. Verify with unit and db tests, including a test that
  no stored column contains the secret.
- [x] 2.3 Implement the authentication dependency (format check, throttle,
  lookup, constant-time compare, status checks, identity in the log context)
  and `GET /v1/projects/self`, with every status documented in OpenAPI.
  Verify with API tests for each `project-authentication` scenario,
  including identical responses for unknown and wrong-secret keys, and
  throttling with and without the trusted header.
- [x] 2.4 Raise the `project_key_rejected` and `auth_failures_throttled` alerts.
  Verify with db tests that one event with the right occurrence count is
  stored and that no key material appears in it.

Expected Result: only valid, active keys reach project endpoints, and abuse is
throttled and reported.

## 3. Admin CLI

- [x] 3.1 Add the `project` subcommands (register, list, show, history,
  set-request, set-profile, remove-profile, enable, disable, rotate-key,
  revoke-key, export, import). Verify with CLI tests for success output,
  one-time key display, and non-zero exits on validation failure.
- [x] 3.2 Add the interactive menu over the same actions, looping until `0` or
  Ctrl+C and re-prompting on invalid input. Verify with menu tests driven by
  scripted input.
- [x] 3.3 Document registration, key rotation, the pepper warning, and the
  tunnel header setting in `README.md` and `AGENTS.md`. Verify that the
  documented commands run against the local stack.

Expected Result: the owner can manage every project with commands or the menu.

## 4. Integration

- [x] 4.1 On the local stack, register a test project, call
  `/v1/projects/self` with each key, rotate with grace, revoke, and disable.
  Record the HTTP results in `verification.md`.
- [x] 4.2 Run `./scripts/check` and record the counts in `verification.md`.

Expected Result: registration through authentication works end to end on the
local stack.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
