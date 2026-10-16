## 1. Definitions and contracts

- [x] 1.1 Add task-specific definitions, dated pricing and discovery schemas; preserve prior digests.
- [x] 1.2 Implement typed bounded requests, effective profile settings and portable configuration.

Expected Result: exact supported fields and immutable vector identity are discoverable.

## 2. Shared execution and native adapters

- [x] 2.1 Share locked admission/credential capture; prove task-conflicting IDs cannot replay.
- [x] 2.2 Add atomic successful settlement/result persistence with rollback coverage.
- [x] 2.3 Add local GPU and stateless OpenAI adapters, strict vectors/usage and certainty mapping.
- [x] 2.4 Add paid token upper-bound reservations and preserve shared grants/cap.

Expected Result: one certain execution and accounting outcome per call, with no fallback.

## 3. Endpoint, client and delivery

- [x] 3.1 Add endpoint/OpenAPI and matching sync/async clients with safe retries.
- [x] 3.2 Exercise API/database/adapter/client matrices without paid network calls.
- [x] 3.3 Verify packaged runtime against local GPU service and record evidence/limitations.
- [ ] 3.4 Run complete gate/strict specs, document operation and publish owner-review PR.

Expected Result: one independently reviewable embedding endpoint change; no deployment.
