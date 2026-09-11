# Frequently asked questions

## Do I need to write YAML?

No. The primary workflow is to describe the behavior you need in **Create**, approve
the relevant context, and review the test Plantain produces.

Plantain stores accepted tests as readable YAML so they can be versioned and
audited. In **Tests**, use **View steps** to understand the generated actions
without learning the file format.

Manual editing is an advanced repository workflow. Validate any reviewed manual
change before running it.

## Does Plantain run the test it creates?

Yes. The accepted creation workflow validates, saves, and runs the generated test.
The live execution appears in the agent workspace, and the immutable outcome is
available in **Runs and Results**.

Plantain may pause first when clarification, discovery, plan approval, or other
grounding evidence is required.

## What does Ready mean?

**Ready** means Plantain can parse the stored test and admit it to the local
validation/run workflow.

It is not a pass result. Open **Runs and Results** to see execution status and
history.

## Why does Plantain discover an interface before automating it?

Discovery gives the agent current evidence about the real UI, API contract, or
database structure. This prevents it from inventing controls, operations, tables,
or relationships from the wording of an intent.

The resulting test remains reviewable before execution.

## Can I inspect what the agent generated?

Yes. Open **Tests**, choose **View steps**, and review each action in order.

A step is one bounded interaction or verification. Environment references identify
where a runtime value will come from without revealing it, and references to an
earlier step show how the test carries evidence forward.

## Which operating systems are supported?

Plantain runs natively on Linux and macOS.

Windows users run Plantain through WSL2 or a Linux container. Native Windows
support requires a dedicated Windows ACL persistence backend that has not yet been
implemented, so Plantain does not weaken its private-artifact protections to run
directly on Windows.

## Do I need to build Plantain before opening the dashboard?

No. After installing the locked dependencies, launch it directly from the
repository:

```bash
uv sync --locked --all-extras
uv run --locked --all-extras plantain --no-dotenv dashboard
```

`uv build` is for producing distribution artifacts, not normal local use.

## Which dashboard address should I open?

Open the loopback address printed by the launcher, normally
`http://localhost:3000`.

The supported launcher uses one port for the interface and its event connection.
You do not need to open a separate API port.

## Can I expose the dashboard to my local network?

The supported Plantain launcher is localhost-only. This protects a developer tool
that can access local tests, evidence, results, and session credentials.

Do not bypass the launcher’s loopback boundary to create a shared service. A shared
deployment would require a separately designed authentication, authorization,
transport-security, and isolation model.

## What does `--no-dotenv` do for the dashboard?

It prevents Plantain from loading values from the project’s `.env` file.

The dashboard still inherits variables already present in its process environment.
You can also enter agent-provider and optional result-integration credentials in
**Settings** for the current backend session.

Those session fields do not supply application or database values referenced by a
test.

## Are credentials saved by the dashboard?

No. Credentials entered in **Settings** remain in backend process memory for the
current session. Plantain does not write them into the project or return them as
browser-readable status.

They disappear when the dashboard process stops.

## Does the agent see my entire repository?

No. Plantain assembles a bounded, sanitized context packet for the current
operation. It may include the intent and explicitly selected tests, results,
contracts, database metadata, or semantic UI evidence required for that workflow.

Review context before sending it to a hosted provider. Use a local provider when
external processing is not approved.

## Can network modes be changed in the dashboard?

No. Network policy applies to the whole runtime and is configured through the
process environment or local `.env`.

Restart the dashboard after changing it. With `--no-dotenv`, set the policy in the
process environment before launch.

## Why was a URL blocked?

Plantain validates the scheme, host, DNS result, resolved address, redirect chain,
and active network policy.

Public HTTPS/WSS is the normal standard-mode path. Private, restricted, database,
Zephyr, and loopback HTTP destinations require their specific approvals. Deny rules
always win.

## Can the agent modify a database?

No. Agent-driven database work is permanently read-only.

Plantain can discover metadata, run one provably read-only query, and verify bounded
evidence. Mutation is rejected even when requested in plain language.

The separate human mutation CLI is intentionally outside agent workflows and is not
an automation workaround.

## Which databases are supported?

Plantain supports PostgreSQL, SQL Server, MySQL, and Oracle through their approved
drivers and runtime policies.

Database credentials must come from environment references, and non-local access
requires the matching network controls.

## Which API descriptions are supported?

Plantain can ground API tests in Swagger 2 and OpenAPI 3 descriptions. It validates
supported operations and schema behavior before generating requests.

Unsupported or ambiguous contract behavior fails explicitly.

## Do API and database tests need a browser?

No. Browser provisioning is lazy and occurs only when a UI test requires it.

## How do I install a browser?

Plantain can lazily install the selected Playwright browser when allowed. To install
Chromium explicitly:

```bash
uv run --locked python -m playwright install chromium
```

Privileged system dependencies remain part of your approved workstation or runner
setup.

## Where are tests stored?

Accepted tests are stored under `scenarios/` in the selected project. The dashboard
discovers the current workspace rather than a global test library.

## Where are results stored?

Native immutable results are stored beneath `output/results/`. The dashboard shows
them in **Runs and Results**.

Detailed traces, compact terminal telemetry, optional Allure files, and other
runtime artifacts use their dedicated paths beneath `output/`.

## Does rerunning overwrite the previous result?

No. Every execution receives a new run identifier and preserves prior local
history.

## Are Allure or Zephyr required?

No. Native local results are authoritative.

Allure and Zephyr Server/Data Center are optional destinations configured in
**Settings**. Zephyr publication requires a real existing `testCaseKey`; Plantain
never invents one.

## Does Plantain create an Allure HTML report?

Plantain emits the raw Allure result files when the integration is enabled. Use the
Allure toolchain separately to serve or generate its presentation.

## Can I run more than one test at a time?

Yes. In **Tests**, enter selection mode or filter the table, then run the approved
scope. The CLI can also run directories, filtered collections, or explicit files.

Plantain preserves deterministic selection and result ordering while enforcing
bounded resource admission.

## Why is some evidence summarized in the dashboard?

Browser-facing views are bounded for usability and safety. A limited preview does
not silently rewrite the source test or convert complete semantic discovery into
partial evidence.

Use the appropriate detail drawer or local authorized artifact when deeper review
is required.

## Can Plantain run in CI?

Yes. Validate reviewed scenarios before granting a job live credentials, then run a
small approved scope in a protected environment.

See [Run Plantain in CI/CD](ci-cd.md).

## How do I stop the dashboard?

Return to the terminal that launched it and press **Ctrl+C**. Session-only
credentials are discarded with the backend process.

## What should I share when asking for help?

Share the safe command shape, Plantain’s sanitized error message, platform and
Python version, safe relative test source, and run identifier when appropriate.

Never share `.env`, resolved secrets, raw target responses, database connection
strings, or unreviewed evidence files.

See [Troubleshooting](troubleshooting.md) for a symptom-based checklist.
