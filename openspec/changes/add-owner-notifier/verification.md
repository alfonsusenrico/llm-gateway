# Verification: add-owner-notifier

Date: 2026-10-13
Branch: `feat/owner-notifier`. The tested revision is the branch head in its
pull request.
Data: synthetic only. Every test alert uses fixed sentences, no personal or
provider data.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1` against local Postgres 16:

- `uv lock --check`, `ruff format --check`, and `ruff check`: passed.
- `pyright`: 0 errors.
- `pytest`: 68 passed, 0 skipped.
- `docker compose config -q`: passed.
- `openspec validate --all`: 3 passed.

Spec coverage (`owner-notifications`):

| Requirement | Tests |
|---|---|
| Alerts are recorded durably | `test_alert_is_recorded_as_pending`, `test_event_raised_while_no_worker_runs_is_delivered_later` |
| Repeats are counted, not re-sent | `test_burst_is_counted_on_one_event`, `test_alert_after_the_window_opens_a_new_event`, `test_concurrent_identical_alerts_share_one_event`, `test_concurrent_alerts_after_the_window_open_exactly_one_new_event`, `test_delivered_event_is_not_sent_again` |
| Raising never affects the caller | `test_database_down_never_raises_to_the_caller`, `test_alert_survives_the_callers_rollback` |
| Retries with backoff, bounded | `test_temporary_failure_is_retried_later`, `test_channel_retry_delay_is_honoured`, `test_permanent_rejection_fails_without_retry`, `test_attempts_are_bounded`, `test_expired_claim_is_retried`, `test_two_workers_never_send_the_same_event_twice`, the Telegram adapter status tests |
| Stale events expire unsent | `test_stale_event_expires_unsent` |
| No content or secrets | `test_transport_error_is_retried_and_never_logs_the_token`, `test_error_response_never_logs_the_token`, `test_long_summary_is_truncated`, `test_token_is_hidden_in_the_settings_repr` |
| Disabled safely without a channel | `test_without_a_channel_events_stay_pending`, `test_disabled_delivery_is_logged_once_and_the_loop_survives_errors`, `test_empty_telegram_values_mean_unconfigured` |
| Owner can test delivery | `test_test_alert_is_delivered`, `test_test_alerts_bypass_deduplication`, `test_without_a_worker_the_test_reports_pending`, `test_rejected_test_reports_failed` |

**Finding fixed during verification:** `httpx2` logs full request URLs at
INFO, and the Telegram token is part of the URL path.
`test_error_response_never_logs_the_token` caught it. Logging setup now caps
the `httpx2` and `httpcore2` loggers at WARNING.

## Local stack (Docker, 2026-10-13)

- **Build:** `docker compose up -d --build` succeeds.
  - The image build had been broken by the `readme` and `license` fields
    added to `pyproject.toml` before the first push. The image now copies
    `README.md` and `LICENSE`.
- **Migration:** `migrate` applied `0001_notifier_events` and exited 0. The
  API is healthy and the worker is running.
- **Worker without a channel:** it logged once, "Alert delivery is disabled:
  no notifier channel is configured."
- **Graceful stop:** `docker compose stop worker` took under 1 s, with exit
  code 0 and the log line "Worker stopped."
- **CLI without a running worker or channel:**
  `docker compose run --rm -T api llm-gateway-admin notifier test` printed the
  `pending` message and exited 1. The event is stored as `info` / `pending`
  with 0 attempts.

## Not verified

- **Task 4.1:** live Telegram delivery. It waits for the owner's notifier bot
  token and chat ID. The adapter is tested only against a mocked transport.
