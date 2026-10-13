# Verification: add-project-contract-registry

Date: 2026-10-13
Branch: `feat/project-contract-registry`. The tested revision is the branch
head in its pull request.
Data: synthetic projects only (`personal_agent`, `smoke_test`, and the
fixtures), with no provider calls.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1` against local Postgres 16:

- lock, format, and lint: passed.
- `pyright`: 0 errors.
- `pytest`: 132 passed, 0 skipped.
- `docker compose config -q`: passed.
- `openspec validate --all`: 3 passed.

### `project-contracts` coverage

| Requirement | Tests |
|---|---|
| Contract version 1 content | `test_valid_contract_round_trips_with_money_as_strings`, `test_missing_environment_request_names_the_field`, `test_registration_stores_version_one_and_two_keys` |
| Environment requests are bounded | `test_ceiling_above_budget_is_rejected`, `test_out_of_range_request_values_are_rejected`, `test_invalid_edit_keeps_the_current_version`, `test_validation_failure_exits_non_zero_with_the_reason` |
| Profiles are structurally valid | `test_unknown_provider_is_rejected`, `test_invalid_profile_name_and_too_many_profiles_are_rejected`, `test_profiles_and_status` |
| Unknown fields are rejected | `test_misspelled_field_is_rejected_with_its_path` |
| Every edit creates a version | `test_every_edit_is_a_version_and_old_versions_stay_readable`, `test_concurrent_edits_become_consecutive_versions`, `test_list_show_and_history` |
| Edits apply without a restart | `test_disabled_project_is_refused` (cache TTL 0); the local stack showed a disable taking effect within 11 s |
| An invalid stored contract affects only its project | `test_corrupted_contract_quarantines_only_its_project` |
| Export and import | `test_import_saves_a_new_version_and_keeps_keys`, `test_import_of_an_unknown_project_is_refused`, `test_import_round_trip_and_unknown_project` |
| Subcommands and interactive menu | `tests/test_admin_projects.py`, `tests/test_admin_menu.py`: re-prompt on invalid input, Ctrl+C, unknown choice, exit with 0 |

### `project-authentication` coverage

| Requirement | Tests |
|---|---|
| Keys issued per environment, shown once | `test_register_prints_both_keys_once`, `test_register_through_the_menu_then_exit` |
| Stored only as keyed hashes | `test_raw_keys_are_never_stored`, `test_issued_key_parses_back_and_matches_its_hash` |
| Bearer authentication | `test_valid_key_identifies_project_and_environment`, `test_missing_header_is_key_missing_with_a_challenge` |
| Malformed keys refused before lookup | `test_malformed_key_is_refused_before_lookup`, `test_malformed_keys_do_not_parse` |
| Invalid keys share one refusal | `test_unknown_and_wrong_secret_keys_get_identical_responses`, `test_key_with_the_wrong_environment_prefix_is_invalid` |
| Failed attempts throttled per source | `test_repeated_failures_throttle_the_source_even_with_a_valid_key`, `test_trusted_header_counts_clients_separately`, `test_header_is_ignored_unless_trusted`, `test_failure_throttle_window_and_crossing` |
| Disabled projects refused | `test_disabled_project_is_refused` |
| Rotation with grace, and revocation | `test_rotation_with_grace_keeps_both_keys_working`, `test_rotation_without_grace_stops_the_old_key`, `test_rotation_sets_grace_on_old_keys_and_revoke_is_immediate`, `test_rotate_and_revoke` |
| Rejections against a known key alert | `test_revoked_key_is_refused_and_alerts_without_key_material` |

## Local stack (Docker, 2026-10-13)

Keys were captured into shell variables and never printed. Only status codes
and response bodies, which contain no key material, were shown.

- **Migration:** `migrate` applied `0002_projects`.
- **Registration and keys:**
  - `llm-gateway-admin project register smoke_test` registered version 1 and
    printed two keys.
  - `GET /v1/projects/self` returned `production` and `development` for the
    respective keys.
  - No header returned 401. `sk-abc` returned `key_malformed`.
- **Rotation and revocation:**
  - `rotate-key --grace-minutes 30`: the old and new keys both returned 200.
  - `revoke-key` on the old key: it then returned `key_invalid`.
- **Disable:** after `disable`, the new key returned `403
  project_disabled` within 11 s.
- **Alerting:**
  - The revoked-key request raised `project_key_rejected` (warning), which
    the worker delivered to Telegram. Its summary names the key ID, project,
    and environment, and no secret.
  - The API and worker logs contain 0 lines with `lgw.` key material.
- **History:** `smoke_test` shows `register` and `disable` versions by actor
  `cli:gateway` (the container user).
- **Cleanup:** `smoke_test` is left disabled. Its remaining keys were never
  printed, and the shell variables were unset.

## Not verified

- Throttling with two or more API processes. The counters are per process,
  as the design accepts. The local stack runs one.
