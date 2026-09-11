# Cross-system testing

Many important outcomes cross a browser, an API, and a database. Plantain can
verify that complete business path in one isolated scenario while keeping each
system’s evidence and security boundary explicit.

You describe the end-to-end outcome. Plantain decides which grounded UI, API, and
read-only database steps are needed to prove it.

## What you provide

Tell Plantain:

- the business journey from start to finish;
- which approved systems participate;
- the starting state or user role;
- the identifier that connects the systems, if known;
- the final observable fact that proves success;
- required environment-reference names.

For example:

> Confirm that a standard customer can submit an order in the storefront, that the
> order API accepts it, and that the matching order can be found through a
> read-only database check using the returned order identifier.

Do not prescribe selectors, request payloads, value-reference syntax, or SQL.
Plantain derives those mechanics from current evidence.

## Choose one authoritative outcome

A cross-system test is easier to trust when it has one clear final question:

- Was the user-visible transaction completed?
- Did the API accept and return the expected resource?
- Does the authoritative data store contain the matching state?

Intermediate checks should explain the path, not compete with the final outcome.
Ask for the smallest sequence that proves the business behavior.

## What Plantain does

### 1. Grounds each system

Plantain observes current UI semantics, supported API operations, and relevant
database metadata before it creates behavior for that system.

### 2. Creates a bounded sequence

The agent orders the necessary actions and checks. It avoids unrelated setup,
duplicate assertions, and data retrieval that does not contribute to the result.

### 3. Passes only required values

When a later step needs an identifier or other output from an earlier step,
Plantain stores that bounded value inside the active scenario and creates a
reference to it. It does not copy the value into a new global store or another
scenario.

### 4. Executes with isolated state

Browser contexts, HTTP cookies, database sessions, scenario values, and observed
secret values remain isolated for each scenario—even when multiple tests run
concurrently.

### 5. Produces one explainable result

The result preserves step order and shows where the end-to-end path passed or
failed. Detailed evidence remains available without crowding the initial summary.

## What you see in View steps

On the **Tests** page, **View steps** lets you review the agent’s generated sequence
before execution.

A step is one action or verification. In a cross-system test, the list helps you
answer:

- Which system does this step interact with?
- What new evidence does it produce?
- Does the next step use only the value it needs?
- Which step proves the final outcome?

An output reference in the generated artifact means “use the safe value produced
by that earlier step.” It does not mean the value is a credential or shared across
tests.

## What to review

Confirm that:

- every participating system is necessary;
- the handoff identifier represents the same business object;
- credentials remain environment references;
- the API sequence uses documented operations;
- the database portion remains one bounded read-only query;
- the final check proves the intent;
- the test can explain a partial failure.

If one system does not contribute meaningful evidence, remove it from the intent
and keep the test focused.

## Understand a failure

A cross-system test may fail after earlier steps succeeded. The result preserves
the completed step evidence and identifies the failure stage without treating the
entire journey as an opaque error.

For example:

- UI passed, API failed: review the request contract and returned status.
- UI and API passed, database check failed: review the identifier handoff,
  expected persistence timing, and read-only query scope.
- Discovery failed before behavior ran: refresh the affected system’s evidence
  rather than changing unrelated steps.

Rerunning creates a new immutable result so you can compare the corrected outcome
with the earlier failure.

## When Plantain pauses

Plantain stops for clarification or correction when:

- the systems cannot be connected by a safe, meaningful value;
- one target is missing or blocked by policy;
- UI, API, or database evidence is stale or unavailable;
- the requested API behavior is unsupported by the contract;
- the database portion would require mutation;
- a handoff would expose or persist a protected value;
- the sequence is too broad to execute or diagnose safely.

Break an overly broad request into smaller outcomes when each can provide useful
confidence independently.

Next, learn how to [manage tests](manage-tests.md),
[run tests](run-tests.md), and [review results](results.md).
