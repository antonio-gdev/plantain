# Execution Engine Specialist

<!--
AGENT METADATA
- Role: Scenario Lifecycle, Isolation, and Ownership Specialist
- Last Updated: 2026-09-02
-->

Read the root instructions first.

## Non-obvious contracts

- Static `activities` and `validate` commands do not load dotenv, create runtime directories, or
  configure logging. Environment and `${step...}` expressions may defer value-dependent checks
  until execution, but malformed activity shape must fail before a target is contacted.
- Activity preparers are async, deduplicated, and run once before repeated steps. Do not provision
  UI, API, or database services merely because the package was imported.
- Every scenario gets a new `ScenarioContext`, `SecretRegistry`, browser/API services, and
  correlation ID. Concurrent tasks must never share values or secret taint.
- Expensive database pools and reporters may be shared by one runner or bounded batch. Ownership is
  explicit: injected/shared resources are not closed by per-scenario services; the creator closes
  them once.
- Recursive selection is deterministic and bounded before loading. Scenario files use
  descriptor-relative no-follow reads, and YAML byte, node, and depth limits apply during
  composition rather than after materializing the graph.
- `run_many` is non-reentrant on one runner. Bounded concurrent workers may finish out of order,
  but returned results preserve selected-file order.
- One admission controller owns scenario, browser, API, database, thread, and process budgets.
  Cancellation never releases a permit before the underlying resource exits; process work is
  terminated and joined, while cooperative filesystem transactions are signaled and drained.
- Spawned workers use the configured memory budget as an absolute address-space ceiling on
  supported non-Darwin Unix systems. Darwin workers derive and immediately restore the smallest
  accepted page-aligned address-space baseline before sealing a hard ceiling at that baseline plus
  the configured budget; never skip the bound because inherited interpreter mappings exceed it.
- Expression resolution observes classified environment-derived values in the scenario secret
  registry before they can enter logs or reports. Internal byte and context-path ceilings remain
  generous and require no additional scenario configuration.
- Store raw activity output only in isolated scenario context. `RunContext.add_operation` sanitizes
  evidence immediately and rejects a sanitizer result that is not a mapping.
- Finalization attempts all resource cleanup and persistence paths. Preserve the primary activity
  failure while aggregating cleanup failures; clear context and secret memory even when reporting
  fails.
- Native report and terminal-event persistence are integrity requirements. Optional Allure and
  remote reporter failures are integration evidence and must not replace a test outcome.
- Keep error messages value-free. Carry structured safe details rather than raw exception strings
  across activity boundaries.
- Unexpected CLI failures expose only a safe exception type, never a raw traceback or message.

## Fast feedback

```bash
uv run --locked --extra dev ruff check \
  src/plantain/engine \
  tests/unit/test_activity_registry.py \
  tests/unit/test_expressions.py \
  tests/unit/test_runner_*.py \
  tests/unit/test_runtime.py \
  tests/unit/test_scenario_*.py
uv run --locked --extra dev pytest -q -p no:cacheprovider \
  tests/unit/test_activity_registry.py \
  tests/unit/test_expressions.py \
  tests/unit/test_runner_*.py \
  tests/unit/test_runtime.py \
  tests/unit/test_scenario_*.py
```
