---
name: create-discovery-scenario
description: Convert a plain-language walkthrough of any web UI into a runnable Python capturePageSnapshot discovery scenario. Use for fresh live page discovery before test analysis or automation generation.
---

# Create a UI Discovery Scenario

Turn the tester's intent into a reviewable, iterative YAML walkthrough. Expose only
`capturePageSnapshot`; do not create application-specific screens, flows, enums, selector modules,
or Python code.

Before authoring or changing a scenario, read
[the complete activity contract](references/capture-page-snapshot.md). It is the source of truth
for supported actions, assertions, locators, frames, popup behavior, and result fields.

## Required workflow

1. Extract the starting URL, intended actions, expected page states, and environment-backed data
   from the prompt. Ask one focused question only when a missing choice materially changes the
   walkthrough.
2. Never request, read, or write literal credentials. Use `env:VARIABLE_NAME` for credentials and
   environment-specific values. If the variable name cannot be inferred safely, ask for it.
3. Read `snapshots/registry.json` before snapshot work and require registry schema 4.0 with a
   top-level `entries` array. Each entry's `activities` indexes verified states and its
   `diagnostics` records failed intent by exact filename. Never infer or edit either collection.
4. Write beneath `scenarios/<team>/discovery/` when a team is known; otherwise use
   `scenarios/discovery/`. Use a lowercase snake_case filename and a separate step for every
   meaningful stable page state.
5. Discover iteratively:
   - First capture the live starting page with `url` and no actions.
   - Read the resulting verified semantic snapshot only after its entry and complete schema-3
     manifest agree and every contained non-symlink chunk matches its byte count and SHA-256.
   - Select the strongest recorded locator candidate for the next user action.
   - Add the next state step, validate it, run it with approval, and inspect its fresh snapshot.
   - If an action or assertion fails and its safe error names `diagnostic_snapshot`, verify and
     inspect that exact diagnostic solely to repair the failed locator or expectation.
   - Rerun the iteration; only successful verification and promotion establishes its activity
     alias as canonical. Repeat until the requested walkthrough is complete.
6. Before every command or logical command group, show the exact command, explain what it does and
   why it is needed now, and wait for explicit approval.
7. Run `plantain validate` before execution. Run `plantain run` only after validation succeeds and
   the user approves live browser interaction.
8. Report the scenario path, verified canonical artifacts, retained failure diagnostics, captured
   states, and unresolved ambiguity. Recommend `identify-critical-decision-space` for the requested
   verified snapshot.

The runtime owns private staging, complete capture, integrity checks, frozen-baseline comparison,
canonical naming, collision-safe history, deprecated redirects, and atomic registry transactions.
Reject legacy registry shapes and unsafe or mismatched evidence; request fresh discovery instead.
Do not mutate the registry, promote evidence manually, or delete capture artifacts in the skill.

## Locator rules

Use candidates from the latest verified canonical snapshot. For the immediate failed iteration
only, the exact diagnostic named by its safe error may ground a repair when no verified state
supports the observed page. Never resolve a diagnostic through an alias or reuse it as canonical
evidence.

Choose candidates in this order:

1. `role` plus accessible `name`
2. `label`
3. `placeholder`
4. `testId`
5. stable visible `text`
6. stable `css`
7. `xpath` only when no safer recorded candidate exists

Never invent a selector. Never use dynamic IDs beginning with a digit, `-` plus a digit, or
`jqg<digits>_`. If multiple elements match, refine the locator or use a snapshot-grounded,
zero-based `nth`; never silently choose the first match.

## Scenario metadata

Preserve user-supplied `JiraTicket`, `testCaseKey`, and `testRunKey` at scenario root. Never
fabricate them. They are optional while publishing is disabled. If the user explicitly requests
Zephyr publication, obtain the required real `testCaseKey`; `JiraTicket` and `testRunKey` remain
optional and must be preserved only when supplied.

Tags are optional execution/reporting metadata. Use `discovery` and a stable application tag when
they help suite selection; directory structure remains the team ownership boundary.

## Minimal example

The second step is retained only after the first fresh snapshot confirms its locators:

```yaml
scenario: "Discover SauceDemo login and inventory"
tags:
  - discovery

steps:
  - capturePageSnapshot:
      id: capture_login
      activity: sauce_demo_login
      url: "env:SAUCE_DEMO_URL"

  - capturePageSnapshot:
      id: open_inventory
      activity: sauce_demo_inventory
      actions:
        - fill:
            target:
              placeholder: "Username"
            value: "env:SAUCE_DEMO_USERNAME"
        - fill:
            target:
              placeholder: "Password"
            value: "env:SAUCE_DEMO_PASSWORD"
        - click:
            target:
              role: button
              name: "Login"
        - waitForUrl:
            value: "**/inventory.html"
      verify:
        - url:
            contains: "inventory.html"

outputs:
  inventoryUrl: "${open_inventory.url}"
  canonicalSnapshot: "${open_inventory.snapshots[0].canonicalFile}"
```

The example demonstrates syntax, not permission to assume those locators for another site or page
state.

## Stop conditions

Stop and explain the exact blocker when:

- required authentication data has no environment reference;
- a fresh verified snapshot or the current failure diagnostic contains no deterministic locator
  for the intended control;
- navigation requires an unsupported browser capability;
- the live UI contradicts the requested walkthrough;
- URL policy blocks the destination.

Do not guess around any stop condition. Report the exact step and action, then refine only from
user input, verified canonical evidence, or the exact diagnostic from the current failed iteration.
