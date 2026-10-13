# Proposal

## Why

Before the gateway can run a call, it must know:

- which models exist;
- what each one accepts (reasoning effort, temperature, output limit,
  inputs);
- what each one costs.

If projects hard-code these rules themselves, the rules drift between
projects and a model upgrade means changing every project. One backend-owned
catalogue and price schedule fixes that, and gives the ledger and admission
(next change) the prices they need.

## Expected Outcome

- `GET /v1/catalogue` lists every model a key may use, with its limits and
  current prices. A project builds its requests from that list, not from
  hard-coded rules.
- Saving a profile that names an unknown model, an unsupported reasoning
  effort, a temperature the model cannot take, or too many output tokens is
  refused with the reason.
- The gateway prices any recorded usage in integer micro-USD from
  effective-dated list prices, including cached input, cache writes, the
  long-context rate, and the service tier.

## What Changes

- **Model catalogue:** a reviewed data file listing, per model:
  - provider and model ID;
  - context window and maximum output;
  - allowed reasoning efforts and the default;
  - the temperature rule (allowed only with effort `none` on gpt-6-luna);
  - input types (text, image, file);
  - service tiers;
  - `development_only` for the stub.

  First entries: `openai/gpt-6-luna`, and `stub/stub-echo` (zero cost,
  development keys only).
- **Price schedule:** a reviewed data file with effective-from dates, per
  model and tier. Each entry holds input, cached input, cache write, output,
  and the long-context threshold and multipliers. The first entries for
  gpt-6-luna (standard and flex) come from OpenAI's pricing page, read
  2026-10-13.
- **A pure cost function:** `cost(usage, model, tier, at)` in micro-USD,
  rounded up. An unknown price gives "unpriced", never zero.
- **Profile validation against the catalogue** when a contract is saved.
- **`GET /v1/catalogue`**, key-authenticated. Development-only models appear
  only for development keys.

## Non-goals

- Executing calls (`add-openai-direct-calls`).
- Admission, ledger, and enforcement (`add-call-admission-and-ledger`).
- Embedding models (phase 5).
- Fetching prices automatically. Prices change only in a reviewed change.

## Capabilities

### New Capabilities

- `model-catalogue`: which models exist, what each accepts, how prices are
  scheduled and applied, and how contracts and projects see the catalogue.

### Modified Capabilities

None. Profile checks against the catalogue are a new requirement in
`model-catalogue`; `project-contracts` structural rules are unchanged.

## Impact

- **Code:**
  - new `catalogue` package (loader, validation, cost function);
  - data files `catalogue/models.json` and `catalogue/prices.json`;
  - a route;
  - a registry service hook for profile validation.
- **Contract and client package:** additive. Contracts gain no fields.
  Existing profiles that fail the new checks keep working until edited,
  because the checks run at save time; at call time an unknown model is
  refused (next changes).
- **Capability:** none new.
