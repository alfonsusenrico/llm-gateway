# Verification: add-function-tools

Date: 2026-10-13
Branch: `feat/function-tools`. The tested revision is the branch head in its
pull request.
Data: synthetic only. The stub model is used throughout the tests; three live
gpt-6-luna tool loops ran on the local stack.

## Automated checks

`./scripts/check` with `REQUIRE_DB=1`:

- lock, format (including code blocks in Markdown), and lint: passed.
- `pyright`: 0 errors.
- `pytest`: 663 passed:
  - 28 gateway tests in `tests/test_function_tools.py` (parametrized
    refusals included);
  - 6 client unit tests in `clients/python/tests/test_client_tools.py`;
  - 1 client contract test running a two-turn loop through the app.
- compose config: passed.
- `openspec validate --all`: 10 passed.

### `llm-calls` coverage

| Requirement | Tests |
|---|---|
| Calls can declare function tools | `test_invalid_tool_requests_are_rejected` (undeclared named `tool_choice`, duplicate names, invalid name, 33 tools, oversized schema, `tool_choice` or `parallel_tool_calls` without tools, unknown continuation item type, an output with no continuation before it, an unknown input item), `test_model_without_function_tools_refuses_tools` (`input_not_supported`), `test_plain_calls_send_no_tool_fields`, `test_tools_change_the_fingerprint` |
| Tool calls are returned with parsed arguments | `test_tool_calls_and_output_items_are_extracted` (mocked OpenAI transport), `test_two_turn_stub_loop`, `test_invalid_argument_json_is_reported_not_fatal` (`invalid_json`, `not_json_object`), `test_replay_returns_the_same_continuation` |
| Loops continue from an opaque continuation | `test_tools_and_second_turn_input_map_to_the_provider` (outgoing order: history, reasoning item with its encrypted content unchanged, call, output), `test_a_second_turn_request_is_accepted`, `test_multi_step_chain_keeps_every_continuation` (three steps, unique stub call IDs), `test_continuation_from_another_model_is_refused`, `test_mismatched_continuation_is_refused_before_admission` (422, no ledger row) |
| Streams announce complete tool calls | `test_stream_announces_calls_and_keeps_completed_reasoning` (the continuation uses items from `output_item.done`, not the completed response's partial reasoning), `test_streamed_tool_call` (events `response.started`, `tool_call`, `response.completed`; same calls and continuation as a direct call) |

### `model-catalogue` coverage

| Requirement | Tests |
|---|---|
| Catalogue records function tool support | `test_packaged_models_support_function_tools`, `test_catalogue_shows_function_tool_support` (`GET /v1/catalogue`) |

### `python-client` coverage

| Requirement | Tests |
|---|---|
| Client helps build tool loops | `test_tool_definition`, `test_function_output_sends_text_or_json`, `test_continuation_round_trips_unchanged`, `test_result_without_tool_calls_has_no_continuation`, `test_results_from_older_gateways_still_parse`, `test_tool_options_are_sent_and_stream_tool_calls_are_typed`, `test_two_turn_tool_loop_with_the_helpers` (contract), and `test_every_gateway_problem_code_is_mapped_by_the_client`, which caught the new `continuation_mismatch` code until the client mapped it |

The client version is now 0.3.0.

## Local stack (2026-10-13)

The image was rebuilt from the working tree (`llm-gateway:local`, image
`93cfe4c3f1b2`). The throwaway project `tools_check` was registered for this
run and is now disabled. Its keys stayed in shell variables and were never
printed.

- **README curl example**, extracted verbatim from `README.md` and run with
  a development key: the first turn returned one `get_events` call with
  `arguments_json` `{"day": "today"}`; the second turn, built with `jq` from
  the first turn's continuation, returned "stub: [\"standup at 9\"]".
- **Client README loop on the stub:** the first step returned a call and a
  continuation (`reasoning`, `function_call`); the second step answered with
  the tool output. A streamed call yielded `Started`, `ToolCall`,
  `Completed`.
- **Live gpt-6-luna loops** (production key, owner-authorised):
  1. Effort `low`, one tool call, then the answer ("Today you have standup
     at 9:00 and a dentist appointment at 13:00."). No reasoning tokens were
     used.
  2. Effort `medium`, "longest free window": again no reasoning before the
     call, so no reasoning item went back. The final step reasoned (97
     tokens) and answered correctly (11:00-13:00).
  3. Effort `medium`, "first day with a 3-hour window": steps 0 and 1 each
     returned an encrypted `reasoning` item plus a `function_call`. Step 2
     sent both continuations back in order, OpenAI accepted them, and the
     model answered correctly ("Tomorrow ... 09:30-17:00"). This exercises
     the main risk: encrypted reasoning passed back with `store=false`.
  - Total live charge: 290 micro-USD (USD 0.00029) for 7 calls, within the
    authorised USD 0.002. Ledger costs match the formula; for example step
    2 of loop 3 is 310 × 0.10 + 54 × 0.50 = 58 micro-USD.
- **Logs:** 0 lines with key material, and 0 with prompt, tool output, or
  reasoning content.

## Findings during verification

- **Loop example error, caught before commit:** the first draft of the
  client README loop sent only the latest continuation from the third step
  on. The provider needs every item since the user's message, so the
  example and the README now keep the whole chain.
  `test_multi_step_chain_keeps_every_continuation` covers the pattern, and
  stub call IDs now continue across a chain so outputs stay unambiguous.
- **For projects:** the model guessed a calendar date (`2026-05-02`,
  `2026-06-19`) when asked about "today". The model does not know the date;
  a project should put it in its instructions. This is project-owned, so the
  gateway does nothing about it.
- **Live runs needed one extra loop:** loops 1 and 2 used no reasoning
  before a tool call, so they never passed reasoning back. Loop 3 was
  designed to need it. The total spend stayed within the authorised budget.

## Not verified

- A live streamed tool call. Streaming tool events are covered by the
  mocked SSE test and the stub, and the live streaming path was verified in
  `add-streaming-responses`.
- A live call where the model sends invalid argument JSON; covered by tests.
- Parallel tool calls in one live step; the loops above made one call per
  step.
