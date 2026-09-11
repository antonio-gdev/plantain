# Plantain Automation

Plantain is a YAML-driven, AI-orchestrated automation framework for any web UI, HTTP API,
Swagger/OpenAPI service, and supported SQLAlchemy database. The framework is intentionally small:
technical users describe intent in plain language, and the AI agent generates reviewable test artifacts, 
while the runtime executes cross-boundary UI, API, and database operations.

Spend more time defining intent, risk, expected behavior, evidence, and guardrails, while AI handles all of the repetitive implementation and execution.

## 🚀 Key Features

*   ✅ **100% Python** – Python is the language for both data science and machine learning, meaning  you can freely use the unmatched ecosystem of libraries to create your own Activites that can be driven through this framework.
*   ✅ **Modular by design** – Simply write your own Python code and add it to the dispatcher contract as an acvitity to be callable.
*   ✅ **No-code test automation** – Full automation across UI, API, and database boundaries.
*   ✅ **AI-driven creation** – The agent generates reviewable test artifacts and maintains reusable framework code/capabilities.
*   ✅ **Bounded project context** – Point the dashboard at selected projects, files, contracts, or business rules for stronger test inference without uploading an entire repository.
*   ✅ **User-controlled log sanitizer** – Clean sensitive data locally before it ever leaves your machine.
*   ✅ **Splunk-ready telemetry** – Emit Splunk-ready JSON telemetry out of the box for enterprise ingestion.
*   ✅ **Optional Jira & Zephyr integration** – Publish test results to existing QA dashboards when enabled.
*   ✅ **Concurrent execution** – Run multiple automated test suites simultaneously to slash execution times.
*   ✅ **Optional Allure results** – Emit raw Allure results out of the box; render HTML with the external Allure CLI.
*   ✅ **Bring Your Own Agent (BYOA)** – Fully compatible with Claude, OpenAI, Copilot, and Gemini.
*   ✅ **Multi-database support** – Easily connect PostgreSQL, MySQL, Oracle, or Microsoft SQL Server.

## User workflow

```text
plain-language intent
        ↓
domain skill discovery and analysis
        ↓
reviewable YAML scenario
        ↓
typed activity validation
        ↓
isolated UI / API / database execution
        ↓
sanitized outputs and canonical artifacts
```

The bundled skills support:

- `create-discovery-scenario`: iteratively navigate any URL and capture current semantic DOM states.
- `identify-critical-decision-space`: derive critical paths, pairwise combinations, boundaries, and state transitions from verified evidence.
- `generate-automation`: compose complete UI, API, Swagger/OpenAPI, and read-only database scenarios; canonical snapshots ground only included UI locators.
- `generate-api-tests`: build positive, negative, boundary, and integration scenarios from Swagger 2 or OpenAPI 3.
- `discover-database`: answer plain-language data questions through phased reflection and bounded parameterized selects.

## Requirements

- Python 3.11 through 3.14
- Linux or macOS; Windows users run Plantain through WSL2 or a Linux container
- `uv` for deterministic dependency and environment management
- Operating-system libraries required by the configured Playwright browser for UI execution
- An optional native database driver matching PostgreSQL, SQL Server, MySQL, or Oracle

## Installation

From the project directory:

```bash
uv sync --locked
```

That installs the CLI runtime. To use the local dashboard from the same source checkout, install
the optional Reflex dependency:

```bash
uv sync --locked --extra dashboard
```

The first UI scenario lazily installs only the configured, Playwright-pinned browser into the
normal OS cache. API and database runs never trigger browser provisioning. Teams may optionally
prewarm an offline or CI environment with `uv run --locked playwright install chromium`; Plantain
never installs privileged operating-system packages automatically.

Framework maintainers can add the development tools with `uv sync --locked --extra dev`. Database
users add only the native driver they need: `--extra postgres`, `--extra mssql`, `--extra mysql`,
or `--extra oracle`. `--extra database` installs all four. SQL Server additionally requires
Microsoft ODBC Driver 18 on the host.

`dev` is the optional linting, typing, security-audit, and test-tool extra used by framework
maintainers. It is not required by scenario authors at runtime. The project does not require an
`--all-groups` installation; add only the database driver a team actually uses. Install
no Python reporting extra: opt-in Allure raw results and Zephyr Scale publishing both use the core
runtime. An Allure command-line installation is needed only when a human or CI job renders the
optional raw results as HTML.

Copy `.env.example` to `.env`, then supply only the environment-specific values you need. Never place real credentials in scenarios or commit them to source control.

The included demonstration targets are:

- UI: `https://www.saucedemo.com/`
- Swagger 2: `https://petstore.swagger.io/v2/swagger.json`

The exact demo hosts are already authorized in `.env.example`. For non-local execution, operators
must enforce an outbound firewall/proxy and set `PLANTAIN_EGRESS_CONTROL_ENFORCED=true` to attest
that deployment control. Database users additionally authorize each endpoint through
`PLANTAIN_DB_ALLOWED_TARGETS` as an exact `host:port`; private endpoints require
`PLANTAIN_DB_ALLOW_PRIVATE_NETWORKS=true`.

## CLI

Validate one scenario or a recursive team directory without contacting a target:

```bash
uv run plantain validate scenarios/api/petstore
```

Execute reviewed files or directories with bounded scenario-level concurrency:

```bash
uv run plantain run scenarios/team-a scenarios/team-b --concurrency 4
```

Select cross-directory suites by optional scenario tags. Repeated `--tag` values must all match;
`--tag-any` adds alternatives; `--exclude-tag` always vetoes a match. Matching is
case-insensitive, while the authored spelling is preserved in logs and result reports.

```bash
uv run plantain run scenarios --tag database --tag smoke --exclude-tag reference
```

Directories remain the team ownership boundary. Tags are an execution/reporting dimension, not a
replacement for recursive team subdirectories.

List the installed YAML-facing activities:

```bash
uv run plantain activities
```

`activities` and `validate` are static commands: they do not load `.env`, create runtime output
directories, or configure runtime logging. `run` continues to support optional local dotenv values
and process environments supplied by CI or a secret manager.

For credential-free command history, export secret values through the shell, CI, or a secret
manager first. These live examples keep only public target locations inline and fail immediately
when a required secret variable is absent:

```bash
SAUCE_DEMO_URL='https://www.saucedemo.com' \
SAUCE_DEMO_USERNAME="${SAUCE_DEMO_USERNAME:?set SAUCE_DEMO_USERNAME}" \
SAUCE_DEMO_PASSWORD="${SAUCE_DEMO_PASSWORD:?set SAUCE_DEMO_PASSWORD}" \
uv run --locked plantain --no-dotenv run \
  scenarios/discovery/saucedemo_checkout_journey.yaml
```

```bash
PETSTORE_API_KEY="${PETSTORE_API_KEY:?set PETSTORE_API_KEY}" \
PETSTORE_SCHEMA_URL='https://petstore.swagger.io/v2/swagger.json' \
uv run --locked plantain --no-dotenv run \
  scenarios/api/petstore/find_available_pets.yaml
```

```bash
PETSTORE_API_KEY="${PETSTORE_API_KEY:?set PETSTORE_API_KEY}" \
PETSTORE_SCHEMA_URL='https://petstore.swagger.io/v2/swagger.json' \
uv run --locked plantain --no-dotenv run \
  scenarios/api/petstore/get_inventory.yaml
```

## Local dashboard

No wheel or source-distribution build is required. After installing the dashboard extra, start the
production-mode local interface directly from the GitHub checkout:

```bash
uv run --locked --extra dashboard plantain --no-dotenv dashboard
```

Plantain prints `http://127.0.0.1:3000`; open that address in a browser. Choose another loopback
port when needed:

```bash
uv run --locked --extra dashboard plantain --no-dotenv dashboard --port 4317
```

The launcher serves the Reflex frontend and Python event backend on the same loopback-only port.
It atomically stages its generated `rxconfig.py`, compiled web workspace, and package-owned PNG
assets below private, Git-ignored `output/dashboard/port-<port>/`. It does not create or execute a
wheel, copy a repository, bind a public interface, or commit generated frontend files.

`--no-dotenv` prevents automatic loading of `PROJECT_ROOT/.env`; it does not clear variables
already supplied by the shell, CI, or a secret manager. Without that flag, dashboard startup uses
the same optional project dotenv behavior as `run`. Agent-provider and optional integration
credentials entered in Settings remain backend-session-only and are never written back to `.env`
or returned to browser state. Scenario-specific `env:NAME` references continue to resolve from
the launcher process environment, so supply those before startup when dotenv loading is disabled.

The primary Create experience accepts plain-language intent, routes to the appropriate built-in
capability without exposing manual skill triggers, asks only for missing information, and presents
reviewable drafts or decision plans. Users can attach locally filtered project directories, files,
API contracts, and business-rule sources to improve inference; the interface shows bounded source
metadata and applies the hosted-provider disclosure boundary before selected content is sent.
Tests, concurrent runs, native results, evidence, analytics, agent usage, and optional reporting
integrations remain available through the same local workspace.

## Optional result publishing

Plantain can publish pass, fail, or skipped outcomes to Zephyr Scale Server/Data Center. It is
internal post-scenario infrastructure, not a YAML activity, and is disabled by default. All three
reporting fields may be omitted when it is disabled. When publishing is enabled, each scenario
requires a real `testCaseKey`; `JiraTicket` remains optional, while `testRunKey` optionally selects
an existing test cycle. Omission or `N/A` means no linked issue or existing run.

Routine publication sends only the validated test case/run keys, status, duration, and a generic
comment; it does not send the scenario name or native report. Complete native-report attachment
upload is a separate opt-in and additionally requires explicit data-governance approval. Remote
failures never replace the scenario's test outcome; a secret-free integration result is retained
in the local scenario report. Zephyr Scale Cloud uses a different API and is not yet supported.

Enabled publication uses a bounded local outbox under `output/reporting-outbox/`. The outbox stores
the token-free request intent, never the Jira token, and uses the framework-owned `correlation_id`
to prevent duplicate Zephyr executions. Only failures proven to occur before sending remain queued;
delivery-ambiguous requests require manual reconciliation and are never retried automatically.
After a current result is confirmed, Plantain retries only the configured number of oldest queued
results. Optional attachments are attempted for the current result only and are never replayed.
The native integration envelope records `delivery_state`, `retry_status`, and the local
`outbox_id`.

Set `PLANTAIN_ALLURE_RESULTS_ENABLED=true` to emit dependency-free raw Allure results under
`output/allure/`. Plantain does not install or invoke the external Allure HTML renderer. Allure
emission and Zephyr publication are optional integrations; required native-result and terminal
telemetry persistence remain fail-closed.

Normal scenario execution never enables database mutation. A separate human-only `db-mutate`
command accepts exactly one reviewed DML file and requires both an environment gate and an exact
acknowledgement phrase. No agent skill or YAML activity can invoke that path.

## Universal UI activity

`capturePageSnapshot` is the sole built-in UI activity. It delegates internally to focused browser, locator, action, assertion, extraction, and registry services.

```yaml
scenario: "SauceDemo - capture the login page"

steps:
  - capturePageSnapshot:
      id: capture_login
      activity: sauce_demo_login
      url: "env:SAUCE_DEMO_URL"
      verify:
        - title:
            contains: "Swag"

outputs:
  finalUrl: "${capture_login.url}"
```

Discovery begins with a navigation-only capture, then uses the fresh canonical snapshot to ground
each following locator. The runtime lazily provisions only the configured Playwright browser;
operating-system browser libraries remain a human-managed deployment prerequisite.

Discovery is iterative: capture a page, read its canonical semantic snapshot, select a recorded
locator candidate, add the next action, and capture the next state. Locator ambiguity is checked
across the complete Playwright match set, and recorded frame paths are not cut off at an arbitrary
depth. YAML document budgets and the scenario deadline provide quiet resource protection without
adding per-scenario count settings. This provides an MCP-like live interaction model without
requiring MCP or application-specific UI code.

### Plain-sentence checkout example

A non-technical tester can give the agent this complete intent without inspecting the DOM:

```text
Go to https://www.saucedemo.com/ and log in using env:SAUCE_DEMO_USERNAME and
env:SAUCE_DEMO_PASSWORD. Wait for /inventory.html and take a snapshot. Add the Sauce Labs
Backpack to the cart, open the cart, and take a snapshot. Verify that the Backpack quantity is
1, then click Checkout. On /checkout-step-one.html, take a snapshot and enter First Name
"Tester", Last Name "QA", and Zip/Postal Code "90210". Click Continue. On
/checkout-step-two.html, verify Item total: $29.99, Tax: $2.40, and Total: $32.39, then click
Finish. Wait for /checkout-complete.html, verify "Thank you for your order!" and "Your order
has been dispatched, and will arrive just as fast as the pony can get there!", and take a
snapshot.
```

The resulting runnable discovery is
[`scenarios/discovery/saucedemo_checkout_journey.yaml`](scenarios/discovery/saucedemo_checkout_journey.yaml).

The console and timestamped JSONL log report navigation, action, snapshot, registration, and
verification progress with one-based indexes and durations. Messages are derived automatically
from the action and its sanitized target; YAML authors do not maintain separate logging prose.
Input values and assertion expectations are included only after centralized recursive redaction
and bounded rendering; observed environment secrets and configured sensitive key fragments are
masked. Raw exception bodies and tracebacks are not included in UI lifecycle messages.

## Logs and end-state telemetry

Plantain writes detailed, per-command diagnostics to a collision-resistant
`output/logger-<timestamp>-p<pid>-<random>.jsonl` file. A diagnostic write failure produces a fixed,
value-free console error without replacing the scenario outcome. Plantain also maintains a separate
Splunk-ready terminal event stream at `output/event/splunk-event-json.log`. That stable file receives
exactly one compact `scenario.completed` JSON record per scenario and rotates at 100 MB with seven
retained backups.

Terminal events contain a generated `correlation_id`, environment, scenario and test-management
metadata, duration, step counts, final status, and bounded failure classification. They never
contain step outputs, API response bodies, database rows, snapshots, input or expected values, or
stack traces. A failed UI operation may include its bounded, sanitized locator target for diagnosis.
The `correlation_id` is framework-owned and distinct from a Zephyr
`integrations.*.execution_id`.

Both log streams pass through built-in redaction and the field names maintained in
`sensitive-keys.txt`. Failure to persist the terminal event is reported as a framework
infrastructure failure instead of silently losing required telemetry.

### Local evidence privacy and retention

Treat detailed logs, native and Allure results, semantic snapshots, registries, outbox entries,
and traces as potentially containing PII, PHI, or payment-related business evidence even after
secret redaction. Plantain stores them with owner-only permissions; permissions are access
control, not encryption or de-identification.

Automatic pruning is limited to evidence whose removal cannot invalidate canonical discovery:
enabled traces default to seven days, 20 archives, and 1 GiB; terminal telemetry rotates at
100 MiB with seven backups; and the token-free Zephyr outbox is count- and document-bounded.
Canonical snapshots and ordinary run projections remain for the project or workspace lifetime
and are never silently deleted. Ephemeral CI workspaces or an organization-owned cleanup policy
should bound their age, count, and aggregate bytes.

For regulated data, keep the workspace and any approved backups on encrypted storage, exclude
transient evidence from backups, and use operating-system, CI-runner, or storage audit controls.
Filesystem deletion does not promise physical overwrite on SSD, copy-on-write, journaled, or
backed-up storage.

Plantain never silently sends an entire repository or raw runtime evidence to an AI provider.
Dashboard agent work locally filters and selects bounded, test-relevant excerpts. Local providers
keep that exchange on the machine; before the first hosted-provider content transfer, the
dashboard presents a clear one-time disclosure. Operator exports and other destinations remain
separate approval decisions.

## Swagger/OpenAPI activities

Schema loading occurs only when a schema workflow requests it. Documents are validated, external
references are blocked by default, and bounded cached content is digest-checked and revalidated on
every load.

- Use `sendRequest` for schema-independent HTTP setup and integration calls.
- Use `loadApiSchema` to discover and content-address the current contract.
- Use `callSchema` for a schema-selected request with optional request/response validation.
- Use `validateSchema` to validate stored evidence or explicitly assert known contract drift.

Schema-download headers and operation headers remain separate so one destination's credential can
never leak to the other. Complete request/response data remains available only in isolated scenario
context; logs and reports receive bounded recursively sanitized evidence.
`callSchema` automatically validates against the request and response media types actually used.
`validateSchema.contentType` is needed only when stored evidence selects among multiple declared
response media schemas.

```yaml
scenario: "Petstore - find available pets"

steps:
  - loadApiSchema:
      id: petstore_schema
      schemaUrl: "env:PETSTORE_SCHEMA_URL"

  - callSchema:
      id: find_available
      schemaId: "${petstore_schema.schemaId}"
      operationId: findPetsByStatus
      headers:
        Accept: application/json
      query:
        status:
          - available
      expectedStatus: 200

outputs:
  statusCode: "${find_available.statusCode}"
  responseBytes: "${find_available.responseBytes}"
```

Swagger 2 base URLs are resolved from `schemes`, `host`, and `basePath`; OpenAPI 3 base URLs are resolved from `servers`. No proprietary schema-URL naming convention or unconditional source-code generation is used.

The checked-in Petstore examples include a strict positive inventory operation and an explicit
expected-invalid public-data contract monitor. Unsupported external references, multipart,
cookie/form-data parameters, response-header contracts, callbacks/webhooks, non-default server
synthesis, server variables, and serialization styles fail closed rather than being guessed.

## Phased database discovery

Agent database access is permanently read-only and deliberately scoped:

1. `phase: schemas`
2. `phase: tables`
3. `phase: table` for complete metadata about exactly one reflected table or view
4. `queryDatabase` for one bounded, parameterized `SELECT` after identifiers are known

Discovery uses SQLAlchemy Inspector/reflection and never requests application rows. Query execution
then accepts one explicit `SELECT`, enforces exact named binds, streams bounded results, and passes
through a SQL-AST firewall that rejects mutation, DDL, locking reads, multiple statements, and
anything it cannot prove read-only.

Plantain also applies vendor read-only transaction controls and statement/driver timeouts where
supported. Deploy it with a least-privilege read-only database account; SQL parsing and transaction
controls are defense in depth, not a replacement for database authorization.

When a query has no root row limit, Plantain quietly adds the effective `maxRows` in the active
dialect while preserving named binds and explicit stricter limits. Batched fetching and aggregate
byte limits bound retained evidence, while the server timeout bounds expensive computation. A
DBAPI driver may materialize an ordinary scalar before returning it; drivers that expose a sized
streamable LOB are rejected before Plantain reads one that cannot fit.

```yaml
scenario: "Find the latest Uno item"

steps:
  - discoverDatabase:
      id: discover_schemas
      source:
        username: "env:APP_DB_USERNAME"
        password: "env:APP_DB_PASSWORD"
        dbUrl: "env:APP_DB_URL"
      phase: schemas

  - discoverDatabase:
      id: discover_tables
      source:
        username: "env:APP_DB_USERNAME"
        password: "env:APP_DB_PASSWORD"
        dbUrl: "env:APP_DB_URL"
      phase: tables
      schema: "Brands"

  - discoverDatabase:
      id: discover_uno
      source:
        username: "env:APP_DB_USERNAME"
        password: "env:APP_DB_PASSWORD"
        dbUrl: "env:APP_DB_URL"
      phase: table
      schema: "Brands"
      table: "Uno"

outputs:
  dialect: "${discover_uno.dialect}"
  tableMetadata: "${discover_uno.tableMetadata}"
```

After reflection confirms the dialect and identifiers, the `discover-database` skill appends a
dialect-correct `queryDatabase` step and returns the requested fact. Queries select an explicit
`value`, `row`, or `rows` result mode; `verifyDatabaseResult` validates the matching shape and
rejects truncated evidence when completeness matters. Each discovery/query step uses the `source`
mapping above. The examples keep real credentials in environment variables, while deliberate
literal synthetic inputs remain accepted and receive the same credential-field redaction.
Runnable examples live under `scenarios/database/clothing/`.

## Security at a glance

Plantain resolves environment references at execution time, recursively redacts credential-shaped
fields and user-classified environment values, validates outbound browser, HTTP, reporting, and
database destinations, and isolates each scenario's state and resources. Non-local deployments
require an externally enforced egress firewall/proxy. Agent-driven database work is strictly
read-only. Semantic snapshots remain private, browser tracing is disabled by default, and Zephyr
publishing and the separate human database mutation command require explicit opt-in.

Owner-only evidence persistence is supported natively on Linux and macOS. Native Windows ACL
support is a future feature; Windows users receive the same protected Linux behavior through WSL2
or a Linux container rather than an insecure permission downgrade.

Redaction removes known secrets but is not de-identification. See
[Local evidence privacy and retention](#local-evidence-privacy-and-retention) for artifact
classification, lifecycle, encryption, backup, deletion, and audit expectations.

## Framework development

Install the complete locked development and database dependency set:

```bash
uv sync --locked --all-extras
```

Before submitting a framework change, run the same local gates enforced by CI:

```bash
uv run --locked --all-extras plantain --no-dotenv validate scenarios
uv run --locked --all-extras ruff check .
uv run --locked --all-extras ruff format --check .
uv run --locked --all-extras mypy src
uv run --locked --all-extras bandit -c pyproject.toml -r src
uv run --locked --all-extras pytest -q -p no:cacheprovider \
  --cov=plantain --cov-report=term-missing --cov-fail-under=80 tests/unit
uv export --locked --all-extras --no-emit-project \
  --output-file /tmp/plantain-audit-requirements.txt
uv run --locked --extra dev pip-audit \
  --requirement /tmp/plantain-audit-requirements.txt \
  --disable-pip --progress-spinner off
uv build
uv run --locked --all-extras python tests/packaging/verify_artifacts.py dist
```

The CI matrix repeats unit tests on Python 3.11, 3.12, 3.13, and 3.14 without contacting live
targets or loading secrets.

## Maintained references

- [`ARCHITECTURE.md`](ARCHITECTURE.md) explains current system boundaries, ownership, security,
  persistence, and extension decisions.
- `activities/` contains the parameter-by-parameter YAML contracts for all eight activities.
- `.agents/skills/` contains the plain-language discovery, analysis, and generation workflows.
- `.env.example` is the complete secret-free configuration template.
