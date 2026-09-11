# Create your first test

Your first Plantain test begins with an outcome, not automation syntax. Use the
dashboard to tell the agent what matters, approve the context it needs, and review
the work as it moves from discovery to a saved test and local result.

## Before you begin

Make sure:

- Plantain is [installed](installation.md);
- you can access the application, API schema, or database you intend to test;
- required application values are already available in the process environment;
- an agent provider is connected in **Settings**.

Do not paste passwords, API keys, database connection strings, or other credentials
into the intent box. Describe environment variable names when the agent needs to
reference protected values.

## 1. Open the dashboard

From the project root:

```bash
uv run --locked --all-extras plantain --no-dotenv dashboard
```

Open `http://127.0.0.1:3000`, then select **Create**.

## 2. Describe the outcome

Write what a person, service, or business process should be able to do. Include the
important starting point and observable result.

Good intents are specific about behavior without prescribing implementation:

=== "UI"

    Confirm that a standard user can sign in to SauceDemo, add the backpack to the
    cart, complete checkout, and see the order confirmation.

=== "API"

    Using the approved Petstore schema, verify that finding available pets returns
    a successful response whose items match the documented pet contract.

=== "Database"

    Verify that the latest UNO clothing item is present in PostgreSQL and report
    only the fields needed to confirm its identity.

=== "Cross-system"

    Confirm that submitting a new order in the web interface produces the expected
    API response and a matching read-only database record.

You do not need to name activities, write selectors, construct requests, or draft
SQL. Plantain determines those mechanics from approved evidence.

## 3. Add context only when it helps

Select **Add context** when the agent needs a specific source, such as:

- an approved application URL;
- a Swagger or OpenAPI schema;
- an existing test or prior result;
- semantic UI evidence from a discovery run;
- database metadata discovered through Plantain.

Context is task-scoped and bounded. Choose the smallest relevant source instead of
sending an entire workspace.

If the intent is already clear, leave context closed and submit the request.

## 4. Follow the agent workspace

The live workspace shows the current stage and the next decision. Depending on the
request, Plantain may:

1. classify the intent;
2. ask one focused clarification;
3. propose a bounded test plan;
4. discover current UI, API, or database facts;
5. create and validate the test;
6. run the accepted test;
7. retain the sanitized result.

Discovery is expected. It is how Plantain avoids inventing page controls, API
operations, or database structure.

## 5. Review before accepting

Review the proposed purpose and scope, not just whether generation succeeded.
Confirm:

- the test answers your original question;
- the target and environment are correct;
- important success and failure outcomes are represented;
- the plan does not include unrelated data or behavior;
- protected values are referenced by environment name rather than embedded;
- a database plan remains read-only.

If something is wrong, describe the correction in business terms. For example:

> Keep the checkout journey, but verify the confirmation number as well as the
> success message.

Plantain retains workflow identity across an approved continuation so the next
proposal remains bound to the same goal and evidence.

## 6. Read the result

After the test is accepted, Plantain validates and saves the generated scenario,
runs it through the shared runtime, and presents the outcome.

Open **Runs and Results** to see:

- pass or failure status;
- duration and completed steps;
- a compact sanitized message;
- detailed step and evidence views when investigation is needed;
- optional publication status for enabled integrations.

Every execution creates a separate local record. A rerun does not replace the
earlier result.

## When Plantain pauses

A pause is a safety or evidence boundary, not a failed generation. Common reasons
include:

- the requested outcome can be interpreted in more than one meaningful way;
- a live target or required environment reference is unavailable;
- UI behavior has not yet been grounded in semantic evidence;
- the requested API operation is absent or ambiguous in the schema;
- a database request would require mutation;
- an outbound target is blocked by network policy.

Answer the clarification, approve suitable context, or correct the environment.
Plantain will not bypass the boundary by guessing.

## Next steps

- Learn how to [describe an effective test intent](../testing/effective-intents.md).
- Understand [UI](../testing/ui.md), [API](../testing/api.md), or
  [database](../testing/database.md) workflows.
- Learn how to [manage tests](../testing/manage-tests.md) after creation.
