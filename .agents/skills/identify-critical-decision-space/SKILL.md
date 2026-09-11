---
name: identify-critical-decision-space
description: "Analyze canonical semantic UI snapshots plus any supplied source, tests, API schemas, or read-only database discoveries to derive the minimum high-value test space: critical paths, pairwise coverage, boundaries, and state transitions. Use after UI discovery and before generating automation."
---

# Identify Critical Decision Space

Create a traceable, minimal test plan from verified evidence. Prefer strong coverage over exhaustive Cartesian combinations.

## Required inputs and lookup

1. Read `snapshots/registry.json` in full and require schema 4.0 with a top-level `entries` array.
2. Resolve every requested activity through one entry's `activities[]` aliases to
   `canonicalFile`; require `evidenceState: verified`.
3. Silently redirect an entry's deprecated filenames to its current canonical file.
4. Before locator use, require a complete schema-3 manifest whose URL, title, structural summary,
   and evidence state match the entry. Reject traversal and symlinks, then verify every referenced
   chunk's byte count and SHA-256 before reading it.
5. Read only successfully verified canonical snapshots for locator and state evidence.
6. If an activity is missing, stop with:

   > No snapshot found for activity `{name}`. Run `/create-discovery-scenario` to capture this page first, then retry generation.

Do not infer controls that are absent from the snapshot.

## Evidence hierarchy

Use all evidence the user has placed in scope:

1. Semantic snapshots: visible controls, constraints, options, navigation, tables, frames, and observed states.
2. Requirements or acceptance criteria: business intent and expected outcomes.
3. Application source and existing tests: hidden validation, authorization, calculation, and persistence rules.
4. Swagger/OpenAPI documents: UI-to-service contracts and error behavior.
5. `discoverDatabase` outputs: schema relationships, persisted state, and current data facts.

Clearly mark inferred behavior and evidence gaps. Database evidence remains read-only.

## Analysis dimensions

Produce all applicable dimensions:

- Critical path: the smallest successful user journeys that prove business value.
- Pairwise: reduce interacting inputs and environment dimensions to pairs while preserving constraints.
- Boundary value: minimum, just below, just above, maximum, empty, null, length, range, and format boundaries supported by evidence.
- State transition: initial state, event, next state, prohibited transition, recovery, refresh, and back-navigation behavior.
- Failure and resilience: authentication, authorization, server errors, timeouts, duplicate submission, retry, and stale state when evidenced.
- Accessibility and semantics: names, roles, focusability, keyboard paths, disabled states, and error announcement behavior.

## Output

Write `test-plans/<feature>.yaml` containing:

```yaml
feature: "Feature name"
sources:
  snapshots: []
  requirements: []
  apiSchemas: []
  databaseDiscoveries: []
assumptions: []
gaps: []
dimensions: []
tests:
  - id: "CP-001"
    category: "critical-path"
    title: "Business-readable title"
    preconditions: []
    steps: []
    expected: []
    evidence: []
    priority: "critical"
```

Every proposed test must cite at least one evidence item. Do not include implementation locators in the plan; automation generation resolves those from canonical snapshots.
