# Tasks

## 1. Request, catalogue, adapters

- [ ] 1.1 Add tools, `tool_choice`, `parallel_tool_calls`, and the
  `function_call_output` and continuation input items (with validation and
  the mismatch check) to the request and resolution, and the
  `function_tools` catalogue flag. Verify with unit tests for every refusal.
- [ ] 1.2 Map tools, outputs, and continuation in the OpenAI adapter, and
  extract tool calls and the full output list. Verify with mocked-transport
  tests that assert the outgoing input order and the untouched reasoning
  items.
- [ ] 1.3 Add stub tool calls and continuation. Verify with tests.

Expected Result: a tool loop's requests and responses map exactly.

## 2. Responses and streaming

- [ ] 2.1 Add `tool_calls` and `continuation` to the response body and to
  stored results, and the `tool_call` stream event (from `output_item.done`).
  Verify with API tests for a direct two-turn stub loop, a streamed tool
  call, invalid argument JSON, and replay including the continuation.
- [ ] 2.2 Document the tool loop (continuation chain per user message) in
  `README.md` and `AGENTS.md`. Verify that the documented example runs with
  the stub.

Expected Result: projects can run tool loops through the gateway.

## 3. Client

- [ ] 3.1 Add the client helpers and typed tool calls. Verify with unit tests
  and a contract test running a two-turn loop against the app.

## 4. Verification

- [ ] 4.1 Run `./scripts/check`. Run one live two-turn gpt-6-luna tool loop
  (owner-authorised, under USD 0.002) and record the result in
  `verification.md`.

## Workflow follow-up

- Report the branch and verification record; the owner decides merge and
  archive.
