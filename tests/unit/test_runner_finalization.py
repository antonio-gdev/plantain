"""Scenario finalization preserves primary failures and clears sensitive state."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

from plantain.config import Settings
from plantain.engine.context import ScenarioContext
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runner import ScenarioRunner
from plantain.engine.runtime import RunContext
from plantain.errors import ActivityExecutionError, AtomicPersistenceError
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.security.secrets import SecretRegistry


class ProbeParams(BaseModel):
    """Minimal parameters for runner boundary tests."""

    id: str


def _settings(tmp_path: Path) -> Settings:
    return cast(
        "Settings",
        SimpleNamespace(
            environment="test",
            sensitive_key_names=("api_token",),
            allow_private_networks=False,
            allowed_hosts=("example.test",),
            scenario_timeout_seconds=2.0,
            step_timeout_seconds=1.0,
            output_dir=tmp_path / "output",
        ),
    )


def _scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario="Runner finalization",
        steps=[StepDefinition(activity="probe", params={"id": "probe"})],
    )


def _registry(handler: Any) -> ActivityRegistry:
    registry = ActivityRegistry()
    registry.register(
        "probe",
        ProbeParams,
        handler,
        description="Exercise the scenario finalization boundary.",
    )
    return registry


def _reject_report(_runner: ScenarioRunner, _result: Any) -> None:
    raise AtomicPersistenceError("private mounted path must not be exposed")


def test_safe_failure_details_retains_only_diagnostic_metadata() -> None:
    context = cast(
        "RunContext",
        SimpleNamespace(secrets=SecretRegistry(sensitive_keys=())),
    )
    failure = cast("Any", RuntimeError("safe UI failure"))
    failure.safe_details = {
        "diagnostic_snapshot": "snapshots/inventory_diagnostic.semantic.json",
        "diagnostic_status": "registered",
        "evidence_state": "diagnostic",
        "raw_payload": "must not pass",
    }

    assert ScenarioRunner._safe_failure_details(failure, context) == {
        "diagnostic_snapshot": "snapshots/inventory_diagnostic.semantic.json",
        "diagnostic_status": "registered",
        "evidence_state": "diagnostic",
    }


def test_report_failure_preserves_primary_error_and_always_clears_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_value = "fixture-protected-value"
    cleared: set[str] = set()
    original_context_clear = ScenarioContext.clear
    original_secret_clear = SecretRegistry.clear

    def clear_context(context: ScenarioContext) -> None:
        original_context_clear(context)
        assert context.export() == {}
        cleared.add("context")

    def clear_secrets(secrets: SecretRegistry) -> None:
        original_secret_clear(secrets)
        assert secrets.redact_text(protected_value) == protected_value
        cleared.add("secrets")

    async def fail_with_secret(context: RunContext, params: ProbeParams) -> None:
        context.values.set_result(params.id, {"api_token": protected_value})
        context.secrets.observe({"api_token": protected_value})
        raise RuntimeError(f"primary activity failure: {protected_value}")

    monkeypatch.setattr(ScenarioContext, "clear", clear_context)
    monkeypatch.setattr(SecretRegistry, "clear", clear_secrets)
    monkeypatch.setattr(ScenarioRunner, "_write_report", _reject_report)
    runner = ScenarioRunner(_settings(tmp_path), _registry(fail_with_secret))

    with pytest.raises(ActivityExecutionError) as raised:
        asyncio.run(runner.run(_scenario()))

    message = str(raised.value)
    assert "primary activity failure" in message
    assert protected_value not in message
    assert "private mounted path" not in message
    assert "configured output filesystem" not in message
    assert cleared == {"context", "secrets"}


def test_report_failure_converts_success_to_safe_framework_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_statuses: list[str] = []

    async def pass_activity(_context: RunContext, _params: ProbeParams) -> dict[str, bool]:
        return {"success": True}

    def capture_terminal(result: Any, **_kwargs: Any) -> None:
        terminal_statuses.append(result.status)

    monkeypatch.setattr(ScenarioRunner, "_write_report", _reject_report)
    monkeypatch.setattr("plantain.engine.runner.emit_scenario_completed", capture_terminal)
    runner = ScenarioRunner(_settings(tmp_path), _registry(pass_activity))

    with pytest.raises(ActivityExecutionError) as raised:
        asyncio.run(runner.run(_scenario()))

    message = str(raised.value)
    assert "Scenario report persistence failed on the configured output filesystem" in message
    assert "private mounted path" not in message
    assert terminal_statuses == ["passed"]


def test_terminal_failure_preserves_persisted_test_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pass_activity(_context: RunContext, _params: ProbeParams) -> dict[str, bool]:
        return {"success": True}

    def reject_terminal(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("private telemetry path must not be exposed")

    monkeypatch.setattr(
        "plantain.engine.runner.emit_scenario_completed",
        reject_terminal,
    )
    runner = ScenarioRunner(_settings(tmp_path), _registry(pass_activity))

    with pytest.raises(ActivityExecutionError) as raised:
        asyncio.run(runner.run(_scenario()))

    message = str(raised.value)
    assert "Scenario telemetry persistence failed on the configured output filesystem" in message
    assert "private telemetry path" not in message
    report_path = next((tmp_path / "output/results/runner_finalization").glob("*.result.json"))
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "passed"
    assert persisted["failure"] is None
