# Dashboard Agent Instructions

<!--
AGENT METADATA
- Role: Local Dashboard and Agent-Experience Owner
- Last Updated: 2026-09-09
-->

## Domain constraints

- Reflex is the presentation layer over Plantain’s typed runtime. Never execute the CLI as a
  subprocess, scrape terminal logs, or duplicate activity behavior in dashboard code.
- The dashboard is a capability-parity surface over the core engine. Browser retention,
  pagination, and rendering may be bounded, but the UI must not reduce supported activities,
  execution concurrency, scenario isolation, cancellation, reporting, or network-policy behavior.
- YAML remains the durable, reviewable automation contract. Visual and conversational authoring
  must compile to the same typed scenario models used by the CLI.
- Users express intent; routing to UI, API, OpenAPI, or read-only database capabilities remains
  internal. Never expose skill names as required product concepts.
- Agent-driven database access remains permanently read-only. Any human mutation utility must stay
  visibly separate and unavailable to agents.

## Security invariants

- Reflex state is browser-serializable. Never place credentials, resolved environment values,
  cookies, authorization headers, raw exceptions, or unredacted evidence in state variables.
- Provider credentials are resolved by the backend from environment variables. Settings may show
  configured, missing, or connection-test status, but never reveal or persist credential values.
- Keep Reflex telemetry and sitemap generation disabled. Bind locally by default and allow only
  explicit localhost origins.
- The frontend compilation process receives an allowlisted environment that excludes application
  and provider credentials.
- Read runtime files with bounded, no-follow persistence helpers. Pass displayed data and errors
  through Plantain’s recursive sanitizer before crossing the backend/frontend boundary.
- Never render arbitrary HTML or expose raw report, snapshot, network, console, database, or model
  payloads without a typed, bounded projection.

## Product and interaction patterns

- Use the geometric logo system without fruit imagery or green branding. Blue is primary, black
  provides structure, yellow communicates active or warning states, and red communicates failures
  or destructive actions.
- Support equally deliberate light and dark themes. Persist only the non-sensitive appearance
  preference in browser storage.
- Use progressive disclosure: approachable defaults first, technical inspectors and advanced
  controls second.
- Explain automation through observable planning, action, verification, and repair evidence.
  Never claim to display hidden chain-of-thought.
- Self-healing is evidence-driven: discover, retain diagnostic evidence, propose a bounded locator
  repair, validate it, rerun, and promote only verified state.
- Guided UI authoring starts with a URL-only discovery scenario. Later iterations may add only
  behavior grounded in the exact verified semantic snapshot, while action or verification
  diagnostics may change only the failed locator.
- Progressive database and UI intents remain in separate bounded backend workflow stores.
  Continuation requires an opaque workflow ID, exact scenario binding or compare-and-rebind, and
  exact run correlation; raw discovery evidence never enters Reflex state.
- Jira, Zephyr, Allure, model providers, and custom endpoints are optional integrations. Their
  absence must not block local authoring, execution, or evidence review.
- Never show fabricated runs, analytics, usage, costs, screenshots, or integration state.

## Performance and ownership

- Load bounded summaries for list views and fetch heavy evidence only when its inspector is opened.
- Build run history from immutable native result files; never reconstruct it from logs or collapse
  distinct correlation IDs into one latest-result row.
- Treat stored context capacity, browser catalog pagination, and per-request agent excerpts as
  separate budgets. Improving attachment usability must never increase provider disclosure.
- When an Overview collection reaches its scan budget, return an explicit lower-bound projection;
  do not make the workspace unavailable or imply that unscanned files do not exist.
- Preserve scenario isolation, deterministic ordering, admission limits, cancellation, and
  exactly-once resource closure from the engine.
- Prefer typed runtime observers for live state. Do not poll or parse log files as an event bus.
- Give every concurrent dashboard job its own bounded latest-snapshot channel; never collapse
  progress into a process-global current run or retain completed channels.
- Live projections may expose correlation, lifecycle status, bounded counts, activity and step
  labels, and operation domain/phase/type only. Targets, inputs, expectations, actual values,
  response bodies, rows, DOM, and exception messages remain in verified evidence.
- Observer or browser-projection failure must not change a scenario outcome. Cancellation still
  propagates to the backend-owned task so the engine retains its normal cleanup boundary.
- Preverify UI run evidence before presenting Continue discovery or Repair locator, then verify it
  again inside the continuation runtime. Never expose a continuation control for an untrusted run.
- Paginate or virtualize unbounded collections and keep provider/model lists explicitly bounded.

## Reflex quirks

- Keep Reflex imports inside this optional dashboard package so CLI-only installations do not
  import the optional dependency.
- Root `assets/` serves source-tree development; packaged dashboard assets are staged from package
  resources by the launcher.
- `plantain dashboard` stages only generated Reflex configuration and package-owned assets below
  private `output/dashboard/port-<port>/`, then invokes Reflex directly with a fixed interpreter
  argument vector.
- Production mode mounts the compiled frontend and ASGI backend on one loopback server. Do not
  introduce a cloud dependency.
- The launcher may inherit the already-selected process environment, but it never logs environment
  values; `--no-dotenv` disables file preloading without disabling backend-session configuration.
- Use documented Reflex APIs only; do not depend on generated `.web/` internals.

## Focused validation order

1. Run dashboard unit tests for pure readers, projections, state transitions, and command wiring.
2. Run Ruff and mypy against changed dashboard and integration files.
3. Compile the Reflex frontend with an environment containing no secret values.
4. Run packaging verification and confirm the wheel contains packaged dashboard assets.
5. Run the repository-wide quality gates before declaring the dashboard complete.

## Anti-patterns

- No CLI subprocess bridge, log scraping, hidden credential copy, fake demo data, dead navigation,
  cloud-only feature, unbounded filesystem scan, or browser-side provider call.
- No forced integration setup, model-cost guess, autonomous database mutation, silent truncation,
  or automatic promotion of unverified healing output.
- No design tokens derived from the plantain fruit.
