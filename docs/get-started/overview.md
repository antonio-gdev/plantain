# What is Plantain?

Plantain is a local-first testing workbench for people who know what their product
should do—even when they do not write automation code.

You describe the behavior in plain language. Plantain’s agent determines what must
be clarified or observed, creates a reviewable test artifact, runs it through the
same bounded runtime used by the CLI, and presents the evidence in the dashboard.

## The Plantain workflow

| Stage | What you do | What Plantain does |
| --- | --- | --- |
| Intent | Describe the outcome that matters | Classifies the request and identifies missing information |
| Context | Approve or add relevant context | Selects bounded local evidence for the active task |
| Plan | Review the proposed scope | Defines the smallest grounded path to the outcome |
| Discovery | Provide access to the approved target | Observes UI, API, or database facts before authoring behavior |
| Test | Confirm the proposal matches your goal | Creates and validates a readable scenario artifact |
| Run | Follow progress | Executes isolated steps and sanitizes retained evidence |
| Result | Review the outcome | Stores an immutable local record and optional integration output |

You remain responsible for the intent, approved access, and review decisions.
Plantain owns the automation mechanics.

## What you see in the dashboard

### Overview

A compact analytics view of the current workspace: test health, recent activity,
result trends, and agent usage. It helps you decide where attention is needed.

### Create

An agent workbench for describing a test, adding optional context, reviewing the
plan, and following creation and execution. The live workspace stays prominent;
the intent composer remains at the bottom like an IDE command surface.

### Tests

A searchable test catalog. Inspect readiness, filter the collection, open generated
steps on demand, select multiple tests, and start execution without navigating
between disconnected screens.

### Runs and Results

An immutable history of local executions. Start with status and duration, then open
the result or evidence drawer only when deeper investigation is useful.

### Settings

Configure the agent, local result behavior, optional integrations, and appearance.
Credentials entered through supported settings remain in the active session rather
than being written into the project.

## What Plantain can test

### UI behavior

Plantain uses semantic browser discovery before it creates interactions. Current
page evidence supports the generated actions and checks, reducing brittle or
invented automation.

### HTTP and OpenAPI behavior

Plantain can make a bounded HTTP request directly or work from Swagger/OpenAPI
metadata. Schema-grounded tests select only supported operations and fail closed on
ambiguous contract behavior.

### Database-backed behavior

Plantain supports PostgreSQL, SQL Server, MySQL, and Oracle. Agent-driven work is
permanently read-only: metadata discovery does not scan application rows, and data
questions use one bounded, parameterized `SELECT`.

### End-to-end business outcomes

One scenario can pass safe, bounded values between UI, API, and database steps.
Every execution keeps browser state, HTTP cookies, database sessions, values, and
secret tracking isolated from other scenarios.

## Two ways to work

=== "Dashboard"

    Best for creating tests from intent, reviewing plans, watching live work,
    exploring the catalog, and investigating results.

    [Launch the dashboard](dashboard.md)

=== "CLI"

    Best for validating or running existing agent-generated scenarios in scripts,
    development workflows, and CI.

    [Use the CLI](cli.md)

The interface changes; the runtime does not.

## What Plantain deliberately does not do

- It does not require users to hand-author test code or YAML.
- It does not send the complete workspace to an agent provider.
- It does not put resolved credentials into generated artifacts.
- It does not allow AI-driven database writes.
- It does not silently relax URL, DNS, schema, size, or timeout policy.
- It does not overwrite an earlier result when a test is run again.
- It does not turn activities into an unrestricted plugin surface.

## Your next step

If Plantain is not installed, follow [Installation](installation.md). If it is
already running, create [your first intent-driven test](first-test.md).
