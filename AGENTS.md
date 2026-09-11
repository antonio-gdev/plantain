# Root Agent Instructions

<!--
AGENT METADATA
- Role: Repository Governance and Domain Router
- Last Updated: 2026-08-30
-->

Plantain is a Python 3.11–3.14, YAML-driven automation framework for UI, HTTP,
Swagger/OpenAPI, and relational-database testing. Plain-language skills create reviewable
scenarios; eight generalized internal activities execute them with isolated scenario state and
bounded shared resources.

## 1. Required workflow

Before any work:

1. Create or truncate `output/agent_command.log` and ensure `.playwright-driver/` exists.
2. Classify the change and read the applicable subtree `AGENTS.md` completely.
3. Verify behavior against current source, activity contracts, scenarios, and tests. Never infer a
   contract from naming or memory.
4. Apply the security invariants below before reading data or proposing execution.
5. Run the smallest domain checks during development, then the full gates before completion.

Before every terminal or system command, show the human the exact command, explain its purpose, and
wait for explicit approval. Do not initialize Git, branch, commit, push, publish, or open a pull
request unless that exact operation is requested.

For files over 500 lines, discover symbols with `rg` and inspect bounded windows. After a major edit, append a three-sentence state checkpoint to
`output/agent_command.log`.

## 2. Sources of truth

Use these in priority order:

1. Typed runtime models and activity registration
2. `activities/{domain}/{activity}.yaml`
3. Runnable `scenarios/`
4. Domain `AGENTS.md` and `ARCHITECTURE.md`
5. Skill instructions under `.agents/skills/`
6. `.env.example`, `sensitive-keys.txt`, `pyproject.toml`, `uv.lock`, and CI

Never inspect `.env`. `sensitive-keys.txt` is safe because it contains field-name fragments only.
Semantic snapshots are private AI evidence: consult `snapshots/registry.json` before snapshot work,
but let runtime transactions own all registry and artifact mutations.

## 3. Domain routing

Read every applicable file for cross-domain changes:

| Change | Required specialist instructions |
|---|---|
| UI activities, browser, locators, actions, assertions, telemetry, snapshots | `src/plantain/activities/AGENTS.md` |
| HTTP client, Swagger/OpenAPI, schema validation | `src/plantain/activities/api/AGENTS.md` plus parent activity instructions |
| Discovery, SQL, drivers, pooling, verification, human mutation boundary | `src/plantain/activities/database/AGENTS.md` plus parent activity instructions |
| Loading, expressions, context, registry, runner, concurrency, ownership | `src/plantain/engine/AGENTS.md` |
| Reflex dashboard, local agent console, visual state, dashboard packaging | `src/plantain/dashboard/AGENTS.md` plus every runtime domain it presents |
| Pydantic/YAML parameter or result contracts | `src/plantain/models/AGENTS.md` |
| Native results, Splunk events, Allure, Zephyr | `src/plantain/reporting/AGENTS.md` |
| Redaction, secret taint, URL/DNS policy | `src/plantain/security/AGENTS.md` |

Changes to observability or persistence require both reporting/engine and security guidance.
Scenario-only changes use the relevant activity/model specialist plus the activity YAML contract.

## 4. Global architecture rules

- Activities are internal framework operations, never plugins.
- The public surface is UI `capturePageSnapshot`; API `sendRequest`, `loadApiSchema`,
  `callSchema`, `validateSchema`; database `discoverDatabase`, `queryDatabase`,
  `verifyDatabaseResult`.
- YAML is the user-facing automation surface. Do not add application screen/flow classes, target
  enums, endpoint builders, generated schema models, or BDD description fields.
- Preserve scenario isolation under concurrency and close shared resources exactly once at their
  owner boundary.
- Complete semantic discovery has no total element/frame limit. Transport chunks and digests must
  never become silent truncation.
- Runtime manifests, registries, caches, reports, and terminal events use atomic persistence.
  Unsupported filesystems fail; they never downgrade to in-place writes.
- Update behavior, activity contracts, examples, architecture, and scoped agent instructions
  together.

## 5. Security boundaries

Security failures are explicit and fail closed. Performance and convenience do not weaken these
rules.

### 5.1 Secrets and logs

- Never open, print, search, copy, or parse `.env`.
- `.env.example` is safe to inspect because it contains names and examples, not credentials.
- `sensitive-keys.txt` is safe to inspect because it contains key fragments, not secret values.
- Accept secrets through `env:NAME`; never place real credentials in YAML, source, logs, reports,
  snapshots, commands, or documentation.
- Resolve environment references only during execution and never serialize their values into YAML.
  Scenario taint follows observed values even when chaining stores them under a different key.
- All log, operation, report, and error evidence passes the central recursive sanitizer. It masks
  matching key/value pairs, configured fragments inside strings, and values observed during
  environment resolution.
- Console and detailed JSONL traces retain action, target, and bounded sanitized
  input/expectation/actual evidence. The Splunk terminal event stays compact.
- Never copy raw exception payloads, HTTP bodies, database credentials, or unredacted browser
  errors into user-facing diagnostics.

### 5.2 Outbound network access

- Standard mode permits public HTTPS/WSS only after blocked-host, URL, DNS, and public-address
  validation. Deny rules always win; normal users do not need to enumerate application
  dependencies.
- Restricted mode requires exact host rules and a real deployment egress firewall/proxy attested
  by `PLANTAIN_EGRESS_CONTROL_ENFORCED=true`; the setting does not create that external control.
- Private/reserved targets require both an exact allow rule and explicit private-network opt-in.
  Plain HTTP/WS is limited to explicitly enabled loopback local development.
- Database destinations use a separate exact `host:port` allowlist and fresh DNS validation per
  pool acquisition. Non-local database and Zephyr access retain deployment egress requirements.
- Revalidate redirects, strip credentials across origins, and block HTTPS-to-HTTP downgrade.
- Block private/reserved targets, external schema references, service-worker bypass, and
  unsupported contract behavior rather than guessing.
- Bound API/reporting responses, redirects, timeouts, connection counts, and safe retries.
- Bound schema documents, database results, parsed YAML structures, execution time, and other
  externally controlled collections before retaining or reporting them.

### 5.3 Runtime isolation

- Isolate browser contexts, HTTP cookies, database sessions/leases, scenario values, and secret
  registries per scenario. Close shared engines and transports exactly once at their owner
  boundary.
- Keep browser tracing disabled by default because trace archives can retain sensitive page data.
- Lazy browser installation remains browser-specific, shell-free, lock-protected, and restricted
  to an allowlisted child environment that excludes application credentials.

### 5.4 Database access

AI-driven database work is permanently read-only.

- `discoverDatabase` performs catalog/metadata discovery and never scans application rows.
- `queryDatabase` permits one provably read-only statement with exact named binds.
- Never generate or execute `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `DROP`, `ALTER`, `CREATE`,
  `TRUNCATE`, procedure calls, locking reads, or equivalent mutations.
- `verifyDatabaseResult` validates value, row, or rows evidence.
- Keep the human mutation CLI outside agent workflows. Never suggest it as an automated
  workaround.
- Preserve TLS verification, SQL parsing/firewall enforcement, rollback-on-return, bounded pools,
  connection timeouts, and per-scenario isolation for PostgreSQL, SQL Server, MySQL, and Oracle.

## 6. Scenario and reporting rules

- Keep YAML declarative and compact. Use `env:NAME` for environment values and
  `${stepId.path}` for scenario-local chaining.
- Directory execution may be concurrent, but selection and returned result order remain
  deterministic. Tags are optional filtering/reporting metadata, not a replacement for directory
  ownership.
- Preserve user-supplied `JiraTicket`, `testCaseKey`, and `testRunKey`. `JiraTicket: N/A` means no
  linked issue. Never fabricate a test case or run key.
- Zephyr publication requires a real `testCaseKey`; `testRunKey` is optional when no existing
  cycle was supplied.
- Raw Allure results belong under `output/allure/`. Detailed logs belong under `output/`; compact
  terminal telemetry belongs under `output/event/splunk-event-json.log`.
- Runtime outputs, browser installs, virtual environments, caches, reports, and snapshots remain
  ignored by Git.

## 7. Bootstrap and full quality gates

Use `uv 0.12.5` with the committed lock. Bootstrap all runtime and development extras:

```bash
uv sync --locked --all-extras
```

Run the final gates from the project root:

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

Parse every checked-in scenario and activity-contract YAML. Live UI, API, database, or Zephyr
checks additionally require human-approved targets, credentials, and environment; restricted or
private targets also require their exact allow rules and deployment controls.

On the current WSL-mounted workspace, direct execution of `.venv/bin/python` may return
`Input/output error`; native WSL `uv run` from the relative project directory is the validated
path. This is a development-mount quirk, not permission to weaken runtime persistence.

## 8. Completion criteria

Do not report completion until:

- The implementation matches current contracts and architecture.
- Applicable targeted checks and every final quality gate pass.
- Security boundaries are explicitly reviewed.
- Behavior changes update activity contracts, examples, architecture, and scoped instructions.
- Checked-in YAML validates and contains no secret values.
- Live validations that could not run are named precisely; missing credentials are not reported as
  success or failure.
- `output/agent_command.log` records the changed files, validation results, and final status.
- Control has returned to the repository root context.
