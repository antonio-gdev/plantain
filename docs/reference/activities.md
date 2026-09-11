# Understand activities

An activity is one generalized internal operation Plantain can place in a generated
test. Activities are the verbs you see in **View steps** and run evidence.

They are not user-installed plugins, application screen classes, or instructions
that a nontechnical user must configure.

## Why activity names are visible

The activity label helps you answer:

- Which system will this step interact with?
- Is this discovery, action, or verification?
- What kind of evidence should this step produce?
- Where did a failed run stop?

Use the label to interpret agent behavior. Describe corrections in business
language rather than choosing activity parameters yourself.

## UI activity

### `capturePageSnapshot`

Opens an approved page, can perform interactions grounded in current semantic
evidence, verifies the requested UI state, and retains complete semantic evidence.

Depending on the intent, one generated UI step may represent:

- URL-only discovery of a new page;
- a grounded continuation of a verified journey;
- a focused repair of a failed target;
- final page-state verification.

Plantain does not create new UI behavior from an invented locator.

## API activities

### `sendRequest`

Makes one bounded direct HTTP request and checks the expected response behavior.
Use its presence to recognize a test that already knows the request it needs.

### `loadApiSchema`

Loads and safely retains an approved Swagger or OpenAPI document. This is the
discovery step for schema-grounded API testing.

### `callSchema`

Selects and calls one operation supported by previously loaded schema evidence.
Its inputs come from the documented contract and approved runtime context.

### `validateSchema`

Checks bounded data against the supported schema contract. It often follows an API
call when the user asked for contract confidence rather than status alone.

## Database activities

### `discoverDatabase`

Discovers schemas, tables, and relevant table metadata without scanning application
rows. This is the evidence-gathering step before database test creation.

### `queryDatabase`

Executes one provably read-only, parameterized, bounded `SELECT` against the
approved source. It cannot perform mutation, procedure calls, or locking reads.

### `verifyDatabaseResult`

Checks the value, row, or bounded rows returned by a prior database query. This is
the step that turns retrieved evidence into a pass or failure.

## Read common sequences

### UI

```text
Discover the page → review grounded behavior → interact and verify
```

The generated artifact may express discovery and later behavior through separate
workflow revisions while preserving the verified action prefix.

### Schema-grounded API

```text
Load schema → call supported operation → validate returned contract
```

Not every API test needs all three activities. A direct known request may use
`sendRequest`.

### Database

```text
Discover metadata → run one read-only query → verify the result
```

Discovery evidence must exist before the agent authors the query.

### Cross-system

```text
Ground each system → perform the smallest useful sequence → verify one authoritative outcome
```

Only the bounded values required by later steps pass through the isolated scenario
context.

## Understand a failure label

When result evidence names an activity, it identifies the kind of operation that
failed. Combine it with:

- step position and ID;
- failure stage;
- expected versus actual evidence;
- artifact references;
- the original test intent.

For example, a `validateSchema` failure usually points to a contract mismatch,
while a `loadApiSchema` failure points to schema access or parsing. A
`verifyDatabaseResult` failure means the query completed but the returned evidence
did not satisfy the expected result.

## Inspect the registered surface

Advanced users can print the registered activity descriptions:

```bash
uv run --locked --all-extras plantain activities
```

The repository’s root `activities/` directory contains the maintainer contracts
used to keep runtime registration, examples, and documentation aligned. Those
contracts are implementation references, not a required user workflow.

Return to [Understand a generated test](generated-artifacts.md) or
[Manage tests](../testing/manage-tests.md).
