---
name: generate-automation
description: Generate complete runnable Python-framework scenario YAML from plain-language intent, composing UI, API, Swagger/OpenAPI, and read-only database activities as needed. Use canonical snapshots only to ground included UI behavior; API-only and database-only scenarios do not require snapshots.
---

# Generate Complete Automation Scenarios

Translate a tester's business intent or approved test plan into maintainable YAML. A scenario may
exercise one domain or chain API setup, UI behavior, schema validation, and database assertions
into one integration or end-to-end test. The YAML is the user-facing automation artifact.

Do not create application-specific Python classes, screens, flows, enums, selector modules,
endpoint builders, or dispatcher cases. Use only framework-owned internal activities already
registered through the activity dispatcher.

## Select evidence by domain

Use the intent and evidence already supplied. Do not require a critical-decision-space plan or a
snapshot when the requested scenario does not need one. Ask one focused question only when a
missing choice would materially change the generated test.

Before generating a step, read its complete typed contract under `activities/`:

- UI: `ui/capturePageSnapshot.yaml`;
- generic API: `api/sendRequest.yaml`;
- Swagger/OpenAPI: `api/loadApiSchema.yaml`, `api/callSchema.yaml`, and
  `api/validateSchema.yaml`;
- database: `database/discoverDatabase.yaml`, `database/queryDatabase.yaml`, and
  `database/verifyDatabaseResult.yaml`.

Generate only parameters and `${step...}` result paths documented by those contracts.

### UI evidence

Use `capturePageSnapshot` for every UI action and assertion. Screens, flow-specific activities,
enums, and maintained selector code are not part of the Python architecture.

When UI behavior is included:

1. Read `snapshots/registry.json` once, require schema 4.0 with a top-level `entries` array, and hold
   it as a frozen baseline.
2. Resolve activity aliases, canonical filenames, and deprecated filenames through the current
   entry. Require verified evidence; if an alias maps to multiple states, ask which is intended.
3. Require a v3 manifest with `captureComplete: true` whose URL, title, structural summary, and
   evidence state match the entry. Resolve every DOM and network chunk beneath `snapshots/`; reject
   traversal and symlinks; verify byte counts and SHA-256 digests; read every chunk in frame and
   sequence order. Accessibility metadata must declare the verified DOM-source contract.
4. Ground every target in the relevant recorded page state. Prefer role plus accessible name,
   label, placeholder, test ID, stable semantic text, stable CSS, then XPath. Confirm uniqueness in
   the correct frame. Never copy a locator merely because it appears in an example.
5. If required UI evidence is absent or ambiguous, stop and request a precise discovery capture.
   Never hallucinate a locator or silently use an unregistered artifact.

Canonical snapshots constrain UI locator generation only. They do not prohibit API or database
steps in the same scenario.

Use an action’s optional `expectResponse` only when user intent or grounded application/API
evidence establishes a causal backend outcome. Provide its explicit URL glob, method, and accepted
status or statuses; never infer unrelated third-party traffic, and leave the field absent when no
causal network proof is needed. Because this expectation retains no body, use an API activity when
response-body behavior itself must be asserted.

### API evidence

Use `sendRequest` for a generic bounded HTTP call when the endpoint, method, input contract, and
expected outcome are established by user intent, existing test data, or application evidence.
Use `loadApiSchema` plus `callSchema` for Swagger/OpenAPI-aware invocation. Use `validateSchema`
when validating a previously obtained response or explicitly asserting observed contract drift.

Do not infer authentication, request fields, operation selection, or expected server behavior.
Keep schema-download headers separate from operation headers. Use exactly one of inline `body` or
project-contained `file` where the activity contract requires it. No schema code generation or
builder pattern is required at runtime.

### Database evidence

The tester should need to provide only environment-backed connection information and, when known,
a useful schema scope. Use `discoverDatabase` ephemerally on the tester's behalf to identify exact
schemas, tables, columns, keys, and relationships through reflection. Normally do not keep these
discovery steps in a durable regression scenario unless metadata discovery is itself under test.

Generate `queryDatabase` only after identifiers and the active dialect are established. Generate
one parameterized, bounded, provably read-only `SELECT` or read-only CTE and choose the narrowest
result mode: `value`, `row`, or `rows`. Assert it with `verifyDatabaseResult` without opening a
second connection. AI-generated scenarios must never contain DML, DDL, locking reads, stored
procedures, multiple statements, or mutation-oriented database activities.

## Compose state and chaining

Order steps by business causality, not by domain. API setup may feed UI navigation; UI or API
results may parameterize a database query; database evidence may provide an expected value for a
later UI or API assertion.

Every reusable step needs a unique literal `id`. Reference only documented result fields:

```yaml
url: "${create_visit.responseBody.visitUrl}"
body: "${create_visit.requestBody}"
parameters:
  visit_id: "${complete_visit.responseBody.visitId}"
actual: "${stored_visit.result}"
```

An expression occupying the complete scalar retains its value type. Embedded expressions must
resolve to scalars. Keep values only in the isolated scenario context and expose the smallest
useful sanitized `outputs` set.

Credentials, tokens, database passwords, private endpoints, and environment-specific values must
use `env:VARIABLE_NAME`; never request, read, or write literal secrets. Use bounded `random:` data
only when synthetic values are appropriate and supported by the intent.

## Generate cohesive scenario files

Write one independent file per test outcome under `scenarios/<team>/<feature>/` when a team is
known, otherwise `scenarios/generated/<feature>/`. Directory structure remains the multi-team
execution boundary. Preserve reporting metadata only when the tester supplies it. If Zephyr
publication is requested, obtain a real `testCaseKey`; preserve optional `JiraTicket` and
`testRunKey` only when supplied. Never fabricate identifiers merely to populate a template:

```yaml
scenario: "Descriptive business outcome"
tags: [integration]

steps: []
outputs: {}
```

Each scenario must establish its own preconditions, contain at least one hard observable business
assertion, and avoid fixed sleeps. Use Playwright auto-waiting, `waitFor`, or `waitForUrl` for UI
state. Keep materially different states in separate scenarios rather than adding runtime branches.
Do not invent cleanup or remote mutations beyond the user's intent; describe external effects
before asking permission to execute.

## Validate and run with human approval

Before every terminal command or logical command group, show the exact command, explain what it
does and why it is required at that stage, and wait for explicit approval.

Use Python commands, never Maven:

```bash
uv run --frozen plantain --no-dotenv validate scenarios/<team>/<feature>/<file>.yaml
uv run --frozen plantain --no-dotenv run scenarios/<team>/<feature>/<file>.yaml
```

Omit `--no-dotenv` only when the user explicitly chooses local dotenv loading. Scenario directory
arguments are recursive and deterministic; add `--concurrency N` only after parallelism is
approved. Validation must pass before execution. Before a live run, enumerate network, browser,
database, and remote-state effects and obtain approval.

On failure, identify the exact activity, step, action/assertion, and safe failure stage. Repair only
from typed contracts and grounded evidence. Stop for missing credentials, unavailable targets,
ambiguous UI controls, unknown API contracts, or undiscovered database identifiers.

Completion requires valid YAML, preserved Jira/Zephyr metadata, correct cross-step chaining,
domain-appropriate evidence, no plaintext secrets, no database mutation, and either a successful
approved execution or a clearly evidenced external blocker.
