# resource-allocation Specification

## Purpose
Turn each project environment's request into a backend-owned grant of requests
per minute, concurrency, monthly budget, and per-call cost ceiling, within the
gateway's capacity and global budget cap, and make every grant visible.

## Requirements

### Requirement: Every active project environment has a grant

The gateway SHALL compute a grant for the `production` and `development`
environments of every active, non-quarantined project, holding granted requests
per minute, concurrency, monthly budget, and per-call cost ceiling.

#### Scenario: New registration

- **WHEN** the owner registers a project
- **THEN** both of its environments have grants within 5 seconds

#### Scenario: Disabled project

- **WHEN** a project is disabled
- **THEN** its grants are removed, and its share returns to the pool

### Requirement: Grants never exceed requests or capacity

For each pooled dimension (requests per minute, concurrency, monthly budget),
no grant SHALL exceed its request, and the sum of grants SHALL NOT exceed the
gateway's capacity for that dimension.

#### Scenario: Undersubscribed pool

- **WHEN** the sum of the requests is within capacity in every dimension
- **THEN** every grant equals its request, and every state is `approved`

#### Scenario: Global budget cap

- **WHEN** the requested monthly budgets add up to 8.00 USD under a 5.00 USD
  cap
- **THEN** the granted budgets add up to at most 5.00 USD

### Requirement: Shortage is shared by guaranteed minimum and weight

When a dimension is oversubscribed, each grant SHALL first receive its request up
to the guaranteed minimum, and the remaining capacity SHALL be shared in
proportion to project weight, capped at each request, with unused share passed
to the others.

#### Scenario: Weighted sharing

- **WHEN** two environments each request 8 concurrency, have weights 3 and 1,
  and capacity is 10 with a minimum of 1
- **THEN** they are granted 7 and 3

#### Scenario: Satisfied project releases its share

- **WHEN** one of two equal-weight environments requests 2 concurrency and the
  other requests 10, with a capacity of 8
- **THEN** they are granted 2 and 6

#### Scenario: Minimums exceed capacity

- **WHEN** the guaranteed minimums of all environments together exceed a
  dimension's capacity
- **THEN** that capacity is split by weight, and the affected grants carry
  the reason `below_guaranteed_minimum`

### Requirement: Cost ceiling follows the granted budget

The granted per-call cost ceiling SHALL be the lower of the requested ceiling
and the granted monthly budget.

#### Scenario: Budget reduced below the ceiling

- **WHEN** an environment requests a 0.50 USD ceiling but is granted a 0.30 USD
  budget
- **THEN** its granted ceiling is 0.30 USD

### Requirement: Allocation is deterministic

The same requests, weights, capacities, and minimums SHALL always produce the
same grants, regardless of registration order.

#### Scenario: Reordered inputs

- **WHEN** the same pool is allocated with the projects listed in a different
  order
- **THEN** every grant is identical

### Requirement: Grants carry a state and reasons

Each grant SHALL have the state `approved` when every dimension equals its
request, and otherwise `reduced`, with a reason code (`capacity_shared` or
`below_guaranteed_minimum`) for each reduced dimension.

#### Scenario: Reduced concurrency only

- **WHEN** only concurrency is granted below the request
- **THEN** the state is `reduced`, with exactly one reason, for concurrency

### Requirement: Grants are recomputed after changes

Grants SHALL be recomputed after any contract, status, or weight change, and
within 60 seconds after the worker starts with changed capacity settings.

#### Scenario: Weight change

- **WHEN** the owner changes a project's weight
- **THEN** the affected grants reflect the new weight before the command
  returns

#### Scenario: Capacity change on restart

- **WHEN** the gateway restarts with a lower concurrency capacity
- **THEN** grants are recomputed within 60 seconds of the worker starting

### Requirement: Projects can read their own grant

`GET /v1/allocation` SHALL return the authenticated project environment's
state, requested and granted values (budget amounts as decimal USD strings),
reasons, profile names, and computation time, and SHALL return `503
allocation_pending` with `Retry-After` when no grant exists yet.

#### Scenario: Reduced grant read

- **WHEN** a project with a reduced grant calls `GET /v1/allocation`
- **THEN** the response shows the requested and granted values side by side,
  with the reasons

#### Scenario: Other projects stay private

- **WHEN** a project calls `GET /v1/allocation`
- **THEN** the response contains only its own environment's grant

### Requirement: Owner sees all grants and is alerted on changes

The admin CLI SHALL show every grant with requested and granted values, states,
reasons, and pool utilisation; a grant becoming reduced or decreasing SHALL raise
a deduplicated warning alert, and a return to `approved` SHALL raise an info
alert.

#### Scenario: Reduction alert

- **WHEN** a new project's registration reduces another project's
  concurrency
- **THEN** the owner receives a warning naming the affected project,
  environment, and dimension

#### Scenario: Restoration alert

- **WHEN** a reduced grant becomes `approved` again
- **THEN** the owner receives an info alert
