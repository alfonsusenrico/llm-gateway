## ADDED Requirements

### Requirement: Client helps build tool loops

The client SHALL provide helpers to declare a function tool, to build a function
call output, and to turn a result's continuation into the next input item, and
SHALL expose tool calls as typed objects in results and stream events.

#### Scenario: Two-turn loop with the stub

- **WHEN** a project runs a call that returns a tool call, then sends the
  continuation and the function output
- **THEN** the second call returns text, with no hand-built provider items
