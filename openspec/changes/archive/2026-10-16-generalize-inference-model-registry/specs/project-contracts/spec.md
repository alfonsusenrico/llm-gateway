## REMOVED Requirements

### Requirement: Contract version 1 content

**Reason**: Alias-backed profiles replace provider/model pairs in the current contract.

**Migration**: Upgrade through the registry to append contract version 2. Keep all
historical version-1 documents readable and preserve project access keys and grants.

## MODIFIED Requirements

### Requirement: Model profiles are structurally valid

A version-2 profile SHALL have a name matching `^[a-z][a-z0-9_-]{1,39}$`, a model
alias string and a parameters object checked against that model's supported
settings. A contract SHALL hold at most 20 profiles. Unknown profile fields SHALL
be rejected. Profiles SHALL NOT contain provider credentials or execution destinations.

#### Scenario: Unknown provider

- **WHEN** a version-2 profile uses legacy provider/model fields
- **THEN** the edit is refused with field-level validation errors

#### Scenario: Model alias profile

- **WHEN** the owner saves a profile with a registered LLM alias and valid parameters
- **THEN** a new contract version is stored and the profile selects that exact alias

### Requirement: Contracts can be exported and imported

The admin CLI SHALL export a project's selected contract version without any key
material, and SHALL import a validated current-schema contract as a new version
without changing project access keys or provider credentials. Historical-schema
imports SHALL require the explicit upgrade path.

#### Scenario: Round trip

- **WHEN** the owner exports the current version-2 contract and imports it
- **THEN** a new equivalent version is stored and all credentials remain unchanged

#### Scenario: Import for an unknown project

- **WHEN** the owner imports a contract whose project is not registered
- **THEN** import is rejected and the owner is told to register the project first

#### Scenario: Historical contract read

- **WHEN** the owner exports a historical version-1 contract after upgrade
- **THEN** its original data is returned without rewriting the historical version

## ADDED Requirements

### Requirement: Contract version 2 preserves project identity and grants

New and upgraded current contracts SHALL use `contract_version: 2`, retain the
immutable project ID, display name, status and requests for both environments,
and support alias-backed profiles. Provider credential material SHALL remain
outside contract JSON.

#### Scenario: New project

- **WHEN** the owner registers a valid version-2 contract
- **THEN** it receives the existing development/production access-key mechanism
  and no provider credential is invented

#### Scenario: Missing environment request

- **WHEN** a contract lacks a required environment request
- **THEN** registration is rejected without storing a partial project

### Requirement: Contract upgrades append versions through the registry

An explicit registry upgrade SHALL map known version-1 provider/model profiles to
fixed aliases, preserve settings and project data, and append a version-2 contract
under the project row lock. Repeating an already completed upgrade SHALL be a
no-op. Unknown mappings SHALL fail without modifying that project.

#### Scenario: Upgrade existing project

- **WHEN** the owner upgrades a version-1 project whose profile mappings are known
- **THEN** a new version is appended, original history/keys/weights/grants/ledger
  remain intact, and no process-wide provider credential is copied

#### Scenario: Unmapped profile

- **WHEN** an old profile has no unique registered alias mapping
- **THEN** upgrade reports its profile identifier, leaves that project unchanged,
  and does not prevent independent projects from being upgraded

#### Scenario: Repeated upgrade

- **WHEN** the owner reruns the upgrade for an already current contract
- **THEN** no duplicate contract version is created

### Requirement: Pending contract upgrades are distinct from malformed contracts

A project whose current contract still uses version 1 SHALL receive non-retryable
`503 contract_upgrade_required` on project API requests until explicitly upgraded.
It SHALL NOT be reported as malformed or invalidate its access key. Other projects
and the registry's historical-read/upgrade commands SHALL remain available.

#### Scenario: Partial local upgrade

- **WHEN** the API runs with one version-1 project and one version-2 project
- **THEN** the former receives `contract_upgrade_required` and the latter continues normally
