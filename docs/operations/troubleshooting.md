# Troubleshooting

Start with the symptom you can see. Plantain intentionally stops when a target,
test, credential source, or evidence record cannot be handled safely.

Do not paste environment dumps, raw responses, database connection strings, or
unredacted browser output into an issue.

## Quick checks

| Symptom | First check |
| --- | --- |
| Dashboard does not open | Confirm the launcher is still running and open the exact loopback address it displays |
| Agent is unavailable | Open **Settings** and review the provider, model, endpoint, and credential status |
| Creation pauses | Read the current agent-stage message; Plantain may need clarification, approved context, or grounded evidence |
| Test is missing | Clear table filters, refresh the workspace, and confirm the file is under `scenarios/` |
| Test is not ready | Open **View steps**, then validate it to see the safe validation message |
| UI test cannot start | Confirm the selected Playwright browser is installed |
| API target is blocked | Check the URL scheme, resolved host, redirects, and active network policy |
| Database test is blocked | Confirm the source environment names, TLS settings, and exact `host:port` policy |
| Result is missing | Confirm the dashboard was launched from the intended project root and `output/` is writable |
| Integration did not publish | Inspect the local result first; optional publication never replaces it |

## Dashboard does not start

From the project root, install the locked dependencies:

```bash
uv sync --locked --all-extras
```

Then launch the dashboard:

```bash
uv run --locked --all-extras plantain --no-dotenv dashboard
```

Plantain prints the address to open. The supported launcher binds the dashboard to
loopback and serves its interface and event connection through one port.

You do not need to run `uv build` before launching from the repository.

### The default port is busy

Choose another loopback port:

```bash
uv run --locked --all-extras plantain --no-dotenv dashboard --port 3001
```

Use the address printed by Plantain rather than constructing a second API address.

### The page opened but cannot connect

- Keep the terminal process running.
- Use the exact scheme, host, and port printed by the launcher.
- Do not replace `localhost` with a LAN address.
- Check whether a local proxy or browser extension is rewriting loopback traffic.
- Stop the launcher with **Ctrl+C**, then start it again.

## The agent is not ready

Open **Settings** and confirm:

- a provider is selected;
- the model name is valid for that provider;
- a required credential is present;
- a local provider is already running;
- a custom provider uses the intended OpenAI-compatible base URL.

If multiple supported environment credentials are detected, select the provider
explicitly instead of asking Plantain to guess.

With `--no-dotenv`, the dashboard does not preload the project’s `.env` file. You
can still enter an agent or result-integration credential in **Settings** for the
current backend session. Application and database values referenced by a test must
already exist in the dashboard process environment.

Session credentials disappear when the dashboard process stops.

## Creation pauses or asks for more information

This normally means the agent cannot safely complete the next stage yet.

Depending on the request, Plantain may need:

- a clearer outcome or boundary;
- one selected existing test or prior result;
- a public API schema URL;
- database discovery from an approved source;
- a UI discovery run that captures the current page;
- review of a proposed plan or generated test.

Answer only the question shown. Do not add credentials or unrelated project data to
the intent.

If a UI workflow changed after discovery, rerun discovery rather than approving
behavior grounded in stale evidence.

## A test is missing from Tests

1. Confirm the dashboard was launched from the intended repository root.
2. Clear search, type, tag, status, and selection filters.
3. Refresh the workspace.
4. Confirm the scenario is under the configured `scenarios/` directory.
5. Run validation from the same project root:

```bash
uv run --locked --all-extras plantain --no-dotenv validate scenarios
```

Plantain discovers tests from the current workspace. It does not merge tests from
unrelated project roots.

## A test is not ready

**Ready** means the stored test can be parsed and admitted to the local
validation/run workflow. It does not mean the test most recently passed.

Use **View steps** to identify the generated action that needs attention, then run
validation. Correct the test through **Create** when possible so the agent can
preserve the intent and required evidence.

## A UI test cannot launch a browser

Plantain can lazily install the selected browser when the environment permits it.
To install Chromium explicitly:

```bash
uv run --locked python -m playwright install chromium
```

This installs the browser binary, not privileged operating-system packages. If
Playwright reports missing system libraries, install those through your approved
workstation or runner-image process.

API-only and database-only tests do not require a browser.

### A UI action or verification fails

Open the exact failed record in **Runs and Results** and inspect its evidence.

For a repairable action or verification failure, return to **Create** with that
result selected as context. Plantain can propose a focused locator repair while
preserving the rest of the accepted workflow.

Navigation, environment, and policy failures may require new discovery or corrected
configuration instead of a locator repair.

## An API or schema test is blocked

Check:

- the target uses an allowed scheme;
- the URL and API key come from the expected environment names;
- DNS resolves to an address permitted by the active network mode;
- redirects remain within approved policy;
- the schema is Swagger 2 or OpenAPI 3;
- external schema references and document sizes are within supported bounds.

Plantain rejects unsupported or ambiguous contract behavior rather than inventing a
request.

## A database test is blocked

Agent-driven database access is read-only. Confirm:

- the source uses environment references rather than literal credentials;
- the driver is installed;
- TLS settings match the approved database;
- private or restricted access has an exact `host:port` rule;
- the query is one provably read-only statement with exact named binds;
- the requested evidence fits the configured row, column, byte, and time limits.

Do not use the human mutation command as a workaround for an agent test.

## A result does not appear

Plantain persists the native local result before optional publication.

Check:

- the dashboard and CLI use the same project root;
- `output/` exists and is writable;
- the filesystem supports Plantain’s atomic persistence requirements;
- the run completed rather than being cancelled;
- active result filters include the run.

Do not edit or replace a native result file. Rerunning a test creates a new immutable
record.

## Allure or Zephyr did not update

Open the local result first. If it exists, execution completed and the issue is in
the optional destination.

For Allure, confirm it was enabled before the run and inspect the configured local
Allure output.

For Zephyr, confirm:

- Zephyr Server/Data Center is the intended product;
- the base URL and Jira credential source are correct;
- the test has a real `testCaseKey`;
- any supplied `testRunKey` identifies the intended cycle;
- the target and deployment egress controls permit publication;
- attachment publication was separately approved if enabled.

Plantain never fabricates Zephyr identifiers and never stores the Jira token in the
publication outbox.

## Network-policy changes do not appear

Network policy is process configuration, not a dashboard control.

Set the approved values in the process environment or local `.env`, then restart the
dashboard. When launched with `--no-dotenv`, only the process environment is used
for network policy.

Changing an agent credential or integration profile in **Settings** does not change
outbound target policy.

## WSL reports an input/output error

On some WSL-mounted workspaces, directly invoking `.venv/bin/python` can return an
input/output error. Run commands through native WSL `uv` from the relative project
directory instead:

```bash
uv run --locked --all-extras plantain --no-dotenv validate scenarios
```

This is a development-mount workaround, not permission to weaken persistence or
security checks.

## Collect a safe problem report

Include:

- the Plantain command with secret values omitted;
- the safe error category and message shown by Plantain;
- the operating system and Python version;
- whether the dashboard or CLI was used;
- the affected test’s displayed name and safe relative source;
- the run identifier when it is safe to share;
- whether the target is public, private, restricted, or loopback.

Do not include:

- `.env` contents or an environment dump;
- resolved credentials;
- raw HTTP bodies;
- database URLs;
- unredacted screenshots, semantic snapshots, or browser errors;
- complete JSONL traces or native result files without review.

If a credential was exposed, rotate it before continuing.

