# Use the dashboard

The Plantain dashboard is the primary workspace for creating tests with the agent,
managing the local catalog, following execution, and reviewing results.

It is a local application. The supported launcher binds the frontend and backend to
the same loopback port and prints the exact URL to open.

## Start the dashboard

From the project root:

```bash
uv run --locked --all-extras plantain --no-dotenv dashboard
```

Open:

```text
http://127.0.0.1:3000
```

The terminal remains attached to the dashboard process. Press ++ctrl+c++ in that
terminal when you are finished.

## Use another port

If port 3000 is already in use:

```bash
uv run --locked --all-extras plantain --no-dotenv dashboard --port 3100
```

Then open `http://127.0.0.1:3100`.

Plantain uses the selected port for both the visual interface and its local API.
There is no second API port to expose when you use the supported launcher.

## Open another Plantain project

Global options come before the `dashboard` command:

```bash
uv run --locked --all-extras plantain \
  --project-root ../another-plantain-project \
  --no-dotenv \
  dashboard
```

Tests, results, evidence, and dashboard runtime are resolved from that project
root.

## Find your way around

### Overview

Use **Overview** to assess the workspace at a glance. It brings together test
health, recent result activity, quality trends, and bounded agent-usage summaries.

### Create

Use **Create** to describe a behavior and work with the agent. Optional context
opens on demand, while planning, discovery, creation, and live execution remain
the primary workspace.

![Create workspace before an agent provider is connected](../assets/images/create-agent-workspace.png)

*Before configuration, Create keeps the intent composer available and directs you
to Agent settings.*

### Tests

Use **Tests** to search and filter the scenario catalog. Enter selection mode for
multi-test work, run one test directly, or open **View steps** to inspect the
generated behavior without leaving the table.

### Runs and Results

Use **Runs and Results** to review immutable local executions. Open a run or its
evidence only when you need detail; the initial list stays compact and
decision-oriented.

### Settings

Use **Settings** to connect the agent, control local result behavior, enable
optional result integrations, and change appearance.

## Navigation and display

On desktop, the left navigation can collapse to icons to give the active workspace
more room. On smaller screens it becomes a full-width navigation drawer opened
from the top bar.

The appearance control is available in the top bar and in **Settings**. Plantain
supports light and dark presentation without changing test or result data.

## Environment and session values

`--no-dotenv` means the launcher does not preload a project `.env` file. It does
not prevent you from:

- passing environment variables to the launch command;
- entering supported agent credentials in **Settings** for the active session;
- entering supported optional-integration credentials for the active session.

For example:

```bash
SAUCE_DEMO_URL="https://www.saucedemo.com" \
SAUCE_DEMO_USERNAME="standard_user" \
SAUCE_DEMO_PASSWORD="secret_sauce" \
uv run --locked --all-extras plantain --no-dotenv dashboard
```

Application credentials are available to the runtime process, but generated tests
retain environment references rather than resolved values. Session-entered agent
and integration credentials are not written into the project.

## Local access boundary

The launcher binds to `127.0.0.1`; it does not listen on `0.0.0.0` or advertise the
dashboard to the local network. Both `127.0.0.1` and `localhost` are accepted
browser origins for the chosen port.

Do not work around the loopback boundary by directly invoking Reflex with broader
host settings. If a team needs a shared service, deploy an explicitly secured
environment rather than exposing a developer dashboard.

## Runtime files

Plantain stages the writable Reflex entry point and packaged logo assets under:

```text
output/dashboard/port-<port>/
```

This runtime directory is ignored source state. The packaged dashboard assets have
one owner inside the Python package and are copied into the local runtime only when
the dashboard starts.

## If the dashboard does not start

- Confirm you ran `uv sync --locked --all-extras`.
- Try another valid port if the selected port is occupied.
- Run the command from the intended project or pass `--project-root`.
- Read the sanitized terminal error; Plantain does not expose raw exception
  payloads at the CLI boundary.
- See [Troubleshooting](../operations/troubleshooting.md) for focused recovery.

Continue with [Create your first test](first-test.md).
