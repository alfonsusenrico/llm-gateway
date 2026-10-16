## 1. Definition and configuration

- [x] 1.1 Add schema-2 local contracts and pinned local definitions while preserving v1 digests; evidence: catalogue and alias-binding regression checks.
- [x] 1.2 Add optional portable URL settings and adapter wiring; evidence: redacted invalid-setting and discovery configuration checks.
- [x] 1.3 Enforce supported fields, message semantics, safe schema references and conservative context bounds; evidence: validation fails before admission/network execution.

Expected Result: configured local models have accurate discovery without changing existing aliases or admitting unsupported requests.

## 2. Native execution

- [x] 2.1 Implement metadata identity checks and bounded direct execution; evidence: synthetic HTTP request captures and completed/incomplete usage checks.
- [x] 2.2 Implement native stream translation with cleanup and an overall deadline; evidence: terminal, truncated, malformed and disconnect checks.
- [x] 2.3 Map confirmed pre-execution refusals, ambiguous sends and configuration problems; evidence: adapter matrix and exhaustive client problem mapping.

Expected Result: local execution preserves gateway response and certainty contracts without CPU or model fallback.

## 3. Endpoint integration and delivery

- [x] 3.1 Exercise actual API/client, grants, zero fees, replay and unknown no-reexecution against disposable PostgreSQL; evidence: focused integration checks.
- [x] 3.2 Run bounded synthetic local-service integration and record source/runtime evidence separately from fakes; no paid call required.
- [x] 3.3 Run ./scripts/check, strict OpenSpec validation and packaged runtime checks; record outcomes and limitations in verification.md.
- [x] 3.4 Report topic branch, review PR and verification record; owner reviews/merges and separately accepts deployment. PR: https://github.com/alfonsusenrico/llm-gateway/pull/17.

Expected Result: one reviewable local LLM change; embedding/OCR execution remain independent subsequent changes.
