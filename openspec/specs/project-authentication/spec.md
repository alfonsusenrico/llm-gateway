# project-authentication Specification

## Purpose
Identify which registered project and environment sent each request, using
project API keys that are safe to use on a publicly reachable gateway.

## Requirements

### Requirement: Keys are issued per environment and shown once

Registering a project SHALL issue one development key and one production key in
the format `lgw.<dev|prod>.<key_id>.<secret>`, with a 256-bit random secret, and
the raw key SHALL be shown only at issuance.

#### Scenario: Registration output

- **WHEN** the owner registers a project
- **THEN** the CLI prints both raw keys once, and no stored value or later
  command reveals them

### Requirement: Keys are stored only as keyed hashes

The gateway SHALL store a key's identifier, project, environment, and an
HMAC-SHA256 of its secret under a gateway-held pepper, and SHALL NOT store or
log the raw key or secret.

#### Scenario: Database inspection

- **WHEN** the key table is read directly
- **THEN** it contains no raw key or secret

### Requirement: Requests are authenticated with a bearer key

Every project endpoint SHALL require `Authorization: Bearer <key>` and SHALL
resolve the project and environment from a valid, unexpired, unrevoked key whose
secret matches in a constant-time comparison.

#### Scenario: Valid production key

- **WHEN** a request carries a valid production key
- **THEN** `GET /v1/projects/self` returns the project ID, the environment
  `production`, and the current contract version

#### Scenario: Missing header

- **WHEN** a request has no `Authorization` header
- **THEN** it is refused with `401 key_missing`

### Requirement: Malformed keys are refused before any lookup

A presented key that does not match the key format SHALL be refused with
`401 key_malformed` without a database lookup.

#### Scenario: Wrong prefix

- **WHEN** a request presents `sk-abc123`
- **THEN** it is refused with `key_malformed`

### Requirement: Invalid keys share one refusal

An unknown, revoked, or expired key, or one with a wrong secret, SHALL be refused
with the same `401 key_invalid` response, so callers cannot tell which key
identifiers exist.

#### Scenario: Wrong secret for a real key ID

- **WHEN** a request presents an existing key ID with a wrong secret
- **THEN** the response is identical to the one for an unknown key ID

### Requirement: Failed attempts are throttled per source

After more than the configured number of failed authentications from one source
address within 60 seconds, the gateway SHALL refuse further requests from that
source with `429 auth_throttled` and `Retry-After: 60`, and SHALL raise one
deduplicated warning alert.

#### Scenario: Guessing client

- **WHEN** a source sends 11 invalid keys within a minute, with the default
  limit of 10
- **THEN** its next request, even with a valid key, receives `429
  auth_throttled`, and the owner receives one alert

#### Scenario: Behind the tunnel

- **WHEN** the trusted client IP header is configured, and two different
  clients arrive through the same proxy
- **THEN** their failures are counted separately, using the header value

### Requirement: Disabled projects are refused

A valid key of a disabled project SHALL be refused with `403 project_disabled`.

#### Scenario: Disabled after issuance

- **WHEN** the owner disables a project whose key is in use
- **THEN** its requests are refused with `project_disabled` within 10
  seconds

### Requirement: Keys can be rotated with a grace period and revoked

Rotating a key SHALL issue a new key for the same project and environment and
SHALL keep the previous key valid only until the chosen grace period ends;
revoking SHALL invalidate a key immediately.

#### Scenario: Rotation with grace

- **WHEN** the owner rotates the production key with 30 minutes of grace
- **THEN** both keys work for 30 minutes, after which only the new key works

#### Scenario: Immediate revocation

- **WHEN** the owner revokes a key
- **THEN** its next request is refused with `key_invalid`

### Requirement: Rejections against a known key alert the owner

Failed authentications against an existing key identifier SHALL raise a
deduplicated warning alert naming the project and environment and never the
key.

#### Scenario: Stale key in a deployment

- **WHEN** a deployment keeps using a revoked key
- **THEN** the owner receives one alert naming the project and environment
  per deduplication window
