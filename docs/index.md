# Start with the behavior that matters

Tell Plantain what needs confidence. Its agent gathers the right evidence, proposes
a test you can review, runs it, and keeps the result local by default.

[Create your first test](get-started/first-test.md){ .md-button .md-button--primary }
[Explore Plantain](get-started/overview.md){ .md-button }

![Plantain Overview showing workspace health, quality, agent usage, and recent runs](assets/images/dashboard-overview.png)

*Overview keeps the current workspace’s quality signals and recent activity in one
decision surface.*

## One testing workspace

Plantain brings test creation, execution, and evidence together without asking you
to become an automation engineer first.

| You want to… | Start here |
| --- | --- |
| Describe a new behavior to test | [Create your first test](get-started/first-test.md) |
| Open the local visual workspace | [Launch the dashboard](get-started/dashboard.md) |
| Run an existing test from a terminal | [Use the CLI](get-started/cli.md) |
| Understand what Plantain does | [Read the overview](get-started/overview.md) |

## Test across the system

### Web interfaces

Describe the journey or outcome. Plantain observes the live interface, grounds
interactions in current semantic evidence, and verifies the state that matters.

[Learn about UI testing](testing/ui.md)

### APIs and OpenAPI

Provide an approved schema or API context and explain the behavior to verify.
Plantain selects supported operations, creates bounded requests, and checks the
contract and response.

[Learn about API testing](testing/api.md)

### Relational databases

Ask a business question in plain language. Plantain discovers metadata first and
uses read-only, bounded queries to verify the answer.

[Learn about database testing](testing/database.md)

### Cross-system journeys

Connect UI, API, and database evidence in one isolated scenario when a business
outcome crosses system boundaries.

[Learn about cross-system testing](testing/cross-system.md)

## A reviewable path from intent to evidence

1. **Describe the outcome.** Say what must work, not how to automate it.
2. **Approve context.** Add only the target, schema, prior result, or evidence the
   agent needs.
3. **Follow the work.** See clarification, planning, discovery, test creation, and
   execution as they happen.
4. **Review the proposal.** Confirm that the generated test matches your intent.
5. **Read the result.** Inspect an immutable local run and open detailed evidence
   only when needed.

Plantain pauses when it cannot proceed safely. It does not invent UI locators,
guess unsupported API behavior, fabricate reporting identifiers, or allow
agent-driven database mutation.

## Dashboard first, CLI when repeatability matters

The dashboard is the primary experience for creating tests with the agent,
understanding live execution, managing the test catalog, and reviewing results.

The CLI runs and validates the same agent-generated scenarios for local scripts and
continuous integration. Both interfaces use the same runtime and security
boundaries.

## Local by default

The dashboard binds to loopback. Scenario files, results, logs, and semantic
evidence stay in the selected project unless you enable an external agent provider
or results integration. Credentials remain in the process environment or supported
dashboard session settings; they are not written into generated test artifacts.

Ready to begin? [Install Plantain](get-started/installation.md).
