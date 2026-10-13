# project-contracts Specification

## Purpose
Define each registered project's contract: who it is, what it requests per
environment, and its model profiles. The owner manages it through the admin
CLI, with every edit kept as a version.

## Requirements

### Requirement: Contract version 1 content

A contract SHALL contain `contract_version` 1, an immutable project ID, a
display name, a status (`active` or `disabled`), a request for each of the
`production` and `development` environments, and zero or more model profiles.

#### Scenario: Valid registration

- **WHEN** the owner registers a project with a valid ID, display name, and
  both environment requests
- **THEN** the contract is stored as version 1 with status `active`

#### Scenario: Missing environment request

- **WHEN** a contract lacks the `development` request
- **THEN** it is rejected with a message naming the missing field, and nothing
  is stored

### Requirement: Environment requests are bounded

Each environment request SHALL hold requests per minute (1-10000), concurrency
(1-100), a monthly budget in USD and a per-call cost ceiling in USD (decimal, at
most 6 decimal places, 0-1000), and the ceiling SHALL NOT exceed the budget.

#### Scenario: Ceiling above budget

- **WHEN** the owner sets a per-call ceiling of 2.00 USD with a monthly budget
  of 1.00 USD
- **THEN** the edit is rejected, and the current version is unchanged

#### Scenario: Out-of-range concurrency

- **WHEN** the owner sets concurrency to 0
- **THEN** the edit is rejected

### Requirement: Model profiles are structurally valid

A profile SHALL have a name matching `^[a-z][a-z0-9_-]{1,39}$`, a provider of
`openai` or `stub`, a non-empty model, and optional reasoning effort, maximum
output tokens (1-128000), and temperature (0-2). A contract SHALL hold at most
20 profiles.

#### Scenario: Unknown provider

- **WHEN** the owner sets a profile with provider `acme`
- **THEN** the edit is rejected

### Requirement: Unknown contract fields are rejected

Contract validation SHALL reject any field it does not define, at every level,
so a typo can never be silently ignored.

#### Scenario: Misspelled field on import

- **WHEN** the owner imports a contract containing `request_per_minute`
- **THEN** the import is rejected with the field's path

### Requirement: Every edit creates a version

Every successful contract change SHALL be stored as a new version with its time,
actor, and change note, and earlier versions SHALL remain readable.

#### Scenario: Request change history

- **WHEN** the owner changes the production concurrency twice
- **THEN** the history lists versions 1, 2, and 3, with times and actors, and
  `show --version 2` returns the middle state

#### Scenario: Concurrent edits

- **WHEN** two edits to the same project are saved at the same moment
- **THEN** both succeed as consecutive versions, and neither overwrites the
  other

### Requirement: Contract edits take effect without a restart

The API SHALL apply a saved contract change, including disabling a project,
within 10 seconds without a restart.

#### Scenario: Disable a project

- **WHEN** the owner disables a project
- **THEN** within 10 seconds, its requests are refused with
  `project_disabled`

### Requirement: An invalid stored contract affects only its project

If a stored contract fails validation on load, the gateway SHALL refuse that
project's requests with `project_quarantined`, SHALL raise an error alert, and
SHALL keep serving every other project.

#### Scenario: Manually corrupted contract

- **WHEN** a project's current contract row is edited directly in the
  database into an invalid shape
- **THEN** that project's requests receive `503 project_quarantined`, other
  projects are unaffected, and the owner is alerted

### Requirement: Contracts can be exported and imported

The admin CLI SHALL export a project's current contract as JSON without any key
material, and SHALL import a validated JSON contract as a new version of an
existing project without changing its keys.

#### Scenario: Round trip

- **WHEN** the owner exports a contract and imports the same file
- **THEN** a new version identical in content is stored, and the keys are
  unchanged

#### Scenario: Import for an unknown project

- **WHEN** the owner imports a contract whose project is not registered
- **THEN** the import is rejected, and the owner is told to register the
  project first

### Requirement: Admin CLI offers subcommands and an interactive menu

The admin CLI SHALL expose every registry action as a subcommand that exits
non-zero on failure, and SHALL open an interactive menu over the same actions
when run without arguments, looping until the owner chooses `0` or presses
Ctrl+C.

#### Scenario: Invalid menu input

- **WHEN** the owner enters an invalid value in the menu
- **THEN** the menu explains the problem and asks again, without exiting

#### Scenario: Scripted failure

- **WHEN** a subcommand fails validation
- **THEN** it prints the reason and exits with a non-zero status
