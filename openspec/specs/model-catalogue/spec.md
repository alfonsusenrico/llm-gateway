# model-catalogue Specification

## Purpose
Define which models the gateway offers, what each accepts, and what each costs,
so requests and contracts are checked against one backend-owned source instead of
rules copied into projects.

## Requirements

### Requirement: Catalogue lists models with their limits

The gateway SHALL hold a reviewed catalogue in which each model has a provider,
model ID, context window, maximum output tokens, allowed reasoning efforts and
default, a temperature rule, accepted input types, service tiers, and whether it
is limited to development keys.

#### Scenario: Invalid catalogue file

- **WHEN** the catalogue file contains an unknown field or a missing value
- **THEN** the gateway refuses to start, naming the problem

### Requirement: Every offered model has a current standard price

Every catalogue model SHALL have a price entry for its standard tier that is in
effect today, and price entries for the same model and tier SHALL NOT overlap in
time.

#### Scenario: Model added without a price

- **WHEN** a model is added to the catalogue without a standard price
- **THEN** the automated checks fail

### Requirement: Cost is computed from effective-dated list prices

The gateway SHALL compute a call's cost in whole micro-USD, rounded up, from the
price entry in effect on the call's UTC date, charging uncached input, cached
input, cache-write input, and output (including reasoning) tokens at their own
rates.

#### Scenario: Cached and cache-write tokens

- **WHEN** a gpt-6-luna call uses 2,000 input tokens, 1,024 of them cached,
  and 500 output tokens
- **THEN** the cost is 976 × 0.10 + 1,024 × 0.01 + 500 × 0.50 USD per
  million tokens, rounded up to the micro-USD (358 micro-USD)

#### Scenario: Price change takes effect on its date

- **WHEN** a new price entry starts on a date
- **THEN** calls before that date use the old price, and calls from that
  date use the new one

### Requirement: Long-context requests are priced at the long-context rate

When a call's input exceeds the model's long-context threshold, the gateway SHALL
apply the long-context input and output multipliers to the whole request.

#### Scenario: Over the threshold

- **WHEN** a gpt-6-luna call has 300,000 input tokens
- **THEN** every input-side rate is doubled and the output rate is multiplied
  by 1.5 for that call

### Requirement: Unknown prices are never treated as free

When no price entry applies, the gateway SHALL report the cost as unknown, never
as zero.

#### Scenario: Missing tier price

- **WHEN** usage is priced for a tier that has no entry
- **THEN** the cost function returns unknown

### Requirement: Profiles are checked against the catalogue when saved

Saving a contract version SHALL fail when a profile names a model not in the
catalogue, a reasoning effort the model does not allow, a temperature the model
cannot take with that effort, or more output tokens than the model allows.

#### Scenario: Temperature with reasoning on gpt-6-luna

- **WHEN** the owner sets a gpt-6-luna profile with effort `low` and
  temperature 0.7
- **THEN** the edit is refused, saying that temperature needs effort `none`
  on this model

#### Scenario: Unknown model

- **WHEN** the owner sets a profile with model `gpt-9`
- **THEN** the edit is refused, saying that the model is not in the
  catalogue

### Requirement: Catalogue changes never quarantine contracts

Loading a stored contract SHALL NOT fail because its profiles no longer match the
catalogue.

#### Scenario: Model later removed

- **WHEN** a catalogue model used by a stored profile is removed
- **THEN** the project still authenticates, and only calls using that
  profile are refused

### Requirement: Projects can read the catalogue for their key

`GET /v1/catalogue` SHALL return, for the authenticated key, every model it may
use with its limits and today's standard prices as decimal USD strings, and
SHALL omit development-only models for production keys.

#### Scenario: Production key

- **WHEN** a production key reads the catalogue
- **THEN** it lists `openai/gpt-6-luna` and not `stub/stub-echo`

#### Scenario: Development key

- **WHEN** a development key reads the catalogue
- **THEN** it lists both models, and the stub prices are zero
