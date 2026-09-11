# Understand a generated test

Plantain saves an accepted test as a readable YAML scenario so it can be reviewed,
validated, versioned, and run consistently. The agent creates this artifact; you
normally inspect it through **Tests** → **View steps**.

You do not need to learn YAML syntax to use Plantain.

## Keep three records distinct

| Record | Question it answers |
| --- | --- |
| Generated test | What will Plantain do and verify? |
| Run result | What happened in one execution? |
| Evidence | What bounded detail supports that result? |

Changing a generated test does not change an earlier result. Rerunning the same
test creates another immutable result.

## Start from the Tests page

Find the test row and read:

- its user-facing name;
- project-relative source;
- readiness;
- UI, API, or database type;
- number of steps;
- organizing tags.

Then choose **View steps**. The drawer reconstructs a sanitized, bounded preview of
the saved test without asking the browser to open an arbitrary project path.

## Understand a scenario

A scenario is one test case. Its top-level information has a small set of meanings:

| Label | How to read it |
| --- | --- |
| Scenario | The behavior this test case is intended to prove |
| Tags | Optional labels for searching, filtering, and reporting |
| Steps | The actions and checks Plantain performs in order |
| Outputs | Selected final values the test is allowed to expose |
| Metadata | Bounded supporting information that does not replace behavior |
| Jira ticket | An existing linked issue, or no issue when explicitly marked `N/A` |
| Test case key | An existing Zephyr test-case identity |
| Test run key | An optional existing Zephyr cycle identity |

Plantain preserves reporting identifiers supplied by the user or existing
artifact. It does not invent them.

## Understand a step

A step is one action or verification. Its position tells you when it runs.

Examples of step meaning include:

- observe a web page;
- perform a grounded browser journey and verify the resulting state;
- load an approved API schema;
- call a documented API operation;
- validate returned data against a schema;
- discover database structure;
- issue one bounded read-only query;
- verify a database value or row result.

The activity name is the concise technical label for that meaning. Focus first on
whether the action belongs in the test and whether its result contributes to the
intended outcome.

## Understand a step ID

Some steps have an ID. Read it as:

> This action has a stable name so a later action can use part of its result.

IDs must be unique within one scenario. They do not create shared state across
tests.

## Understand environment references

When a preview shows:

```text
env:VARIABLE_NAME
```

read it as:

> Obtain this value from the runtime environment when the test runs.

The artifact contains the name only. It should never contain the resolved password,
token, connection string, or other protected value.

## Understand earlier-step references

When a later step refers to an earlier step and result path, read it as:

> Use this specific bounded value produced earlier in the same test.

This lets an API identifier, safe browser output, or query value support a later
check without copying it into global state.

The reference is resolved only during that scenario’s execution. A missing path or
unsupported value fails explicitly.

## Read an ordered flow

Review steps from top to bottom and ask:

1. Does the first step establish the correct starting context?
2. Does each action move toward the named outcome?
3. Does discovery happen before Plantain relies on unknown structure?
4. Does each value handoff have a clear purpose?
5. Does the final check prove the scenario name?

For a UI journey, the test may begin with observed page evidence and continue with
grounded behavior. For an API test, schema loading may precede an operation call
and response validation. For a database test, metadata discovery should precede
the read-only query and verification.

## Read redaction and display notices

The preview may show:

- a redaction marker where protected text was removed;
- a notice that one step preview reached its display limit;
- pagination when the test has more steps than fit in one drawer page.

These notices describe the review surface. They do not silently shorten the saved
scenario or execution.

If a literal secret appears instead of a redaction or environment reference, do
not run the test. Remove the artifact from use and recreate it from a safe intent.

## Read readiness correctly

Ready means Plantain can load and validate the generated definition against the
current activity contracts. It does not mean:

- the live target is reachable;
- environment values are present;
- the latest run passed;
- an external result integration is available.

Use **Runs and Results** for execution status.

## If the generated behavior is wrong

Return to **Create** and explain:

- what should stay;
- what should change;
- what result should prove the correction.

For example:

> Keep the schema inspection and available-pets request. Change the final check so
> an empty response fails the test.

Plantain can then create a revised, validated artifact from the corrected intent.

## If an advanced user edits a file

Generated scenarios are reviewable source artifacts, so an authorized advanced
user may inspect them in the repository. After any intentional external edit:

1. refresh the **Tests** catalog;
2. reopen **View steps**;
3. validate the affected test;
4. confirm that no literal secret was introduced;
5. run it as a new execution.

The dashboard rejects stale identities rather than silently running a different
file under an old selection.

See [Manage tests](../testing/manage-tests.md) for catalog actions and
[Activities](activities.md) for the plain-language activity map.
