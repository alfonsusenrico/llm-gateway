## ADDED Requirements

### Requirement: Catalogue records function tool support

Each catalogue model SHALL state whether it supports function tools, and
`GET /v1/catalogue` SHALL show it.

#### Scenario: Catalogue read

- **WHEN** a project reads the catalogue
- **THEN** gpt-6-luna shows `function_tools: true`
