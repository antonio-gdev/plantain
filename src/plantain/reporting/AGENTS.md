# Reporting Specialist

<!--
AGENT METADATA
- Role: Native, Allure, Zephyr, and Terminal Reporting Specialist
- Last Updated: 2026-09-02
-->

Read the root instructions first. Also read the security domain instructions for any payload or log
change.

## Non-obvious contracts

- Each execution persists one immutable, correlation-ID-keyed native result JSON as canonical.
  Determine test outcome and owned-resource cleanup before projections; its persistence failure is
  a framework failure.
- Detailed console/JSONL diagnostics retain ordered operation traceability: action, target, and
  bounded sanitized input/expected/actual evidence. Each command owns a collision-resistant
  per-process file. Write failures emit a fixed value-free console error without replacing the
  scenario outcome.
- The rolling Splunk stream receives exactly one schema-versioned terminal event after final state
  is known. Keep it low-cardinality: no step outputs, HTTP bodies, database rows, snapshot content,
  input/expected/actual payloads, stack traces, or raw exception messages.
- `correlation_id` joins diagnostics and terminal state. It is not a Zephyr execution ID.
- Allure emission is dependency-free and opt-in. Emit raw files under `output/allure`, preserve
  stable history identity across reruns, and exclude raw bodies, rows, snapshots, screenshots,
  exceptions, and tracebacks. An emitter failure is recorded but does not alter test status.
- Zephyr support is Server/Data Center v1 only. Cloud is a different API and must fail as
  unsupported rather than reuse these endpoints.
- Reporting is disabled by default, so all reporting metadata may be omitted. Enabled Zephyr
  publishing requires only a real `testCaseKey`; `JiraTicket` and `testRunKey` remain optional,
  and `N/A` means no linked issue or existing run. Do not create or modify Jira issues.
- Publication and attachment are separate opt-ins. Redirects and transport-level POST retries stay
  off; response bodies are bounded and discarded. Remote failure records a value-free stage/status
  and never replaces the scenario outcome.
- Enabled Zephyr publication must atomically enqueue a bounded token-free intent before one
  cross-process claimant sends it. Retry only jobs proven not sent; retain ambiguous delivery for
  manual reconciliation, and never resend delivered, rejected, or `sending` jobs.
- Drain only the configured oldest queued batch after the current result is confirmed. Revalidate
  stored intent with the current token and URL policy. Never store credentials or replay
  attachments.
- Report attachments use only the bounded sanitized native result. Never attach raw execution
  context or artifacts.
- Concurrent runs may share one bounded reporting transport; the owning runner/batch closes it
  once.

## Fast feedback

```bash
uv run --locked --extra dev ruff check \
  src/plantain/reporting \
  src/plantain/engine/reporting.py \
  tests/unit/reporting \
  tests/unit/test_runner_reporting.py \
  tests/unit/test_reporting_configuration.py
uv run --locked --extra dev pytest -q -p no:cacheprovider \
  tests/unit/reporting \
  tests/unit/test_runner_reporting.py \
  tests/unit/test_reporting_configuration.py \
  tests/unit/test_observability.py
```

Live Zephyr validation requires an explicitly approved Server/Data Center endpoint and real
environment-backed credentials. Unit success is not evidence of remote deployment compatibility.
