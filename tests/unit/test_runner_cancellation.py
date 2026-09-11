"""Scenario cancellation owns cleanup and never publishes an in-flight result."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

from plantain.config import Settings
from plantain.engine import runner as runner_module
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runner import ScenarioRunner
from plantain.engine.runtime import ExecutionServices, RunContext
from plantain.errors import ActivityExecutionError
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.reporting import PublicationResult
from plantain.security.secrets import SecretRegistry


class ProbeParams(BaseModel):
    id: str


class RecordingReporter:
    provider = "recording"

    def __init__(self) -> None:
        self.statuses: list[str] = []

    def validate_scenario(self, _scenario: ScenarioDefinition) -> None:
        pass

    async def publish(
        self,
        _scenario: ScenarioDefinition,
        *,
        status: str,
        duration_ms: int,
        report: Mapping[str, Any],
        secrets: SecretRegistry,
    ) -> PublicationResult:
        del duration_ms, report, secrets
        self.statuses.append(status)
        return PublicationResult(provider=self.provider, status="recorded")

    async def close(self) -> None:
        pass


def _settings(tmp_path: Path, *, cleanup_timeout_seconds: float = 0.1) -> Settings:
    return cast(
        "Settings",
        SimpleNamespace(
            environment="test",
            sensitive_key_names=(),
            allow_private_networks=False,
            allowed_hosts=("example.test",),
            scenario_timeout_seconds=1.0,
            step_timeout_seconds=1.0,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
            output_dir=tmp_path / "output",
            allure_results_enabled=False,
        ),
    )


def _scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario="Cancellation ownership",
        steps=[StepDefinition(activity="probe", params={"id": "probe"})],
    )


def _registry(handler: Any) -> ActivityRegistry:
    registry = ActivityRegistry()
    registry.register(
        "probe",
        ProbeParams,
        handler,
        description="Cancellation boundary probe.",
    )
    return registry


def test_external_cancellation_cleans_up_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    exited = asyncio.Event()
    cleanup_flags: list[bool] = []
    writes: list[str] = []
    terminal_events: list[str] = []
    reporter = RecordingReporter()

    async def block(_context: RunContext, _params: ProbeParams) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    async def close(_services: ExecutionServices, *, failed: bool) -> None:
        cleanup_flags.append(failed)

    monkeypatch.setattr(ExecutionServices, "close", close)
    monkeypatch.setattr(
        ScenarioRunner,
        "_write_report",
        lambda _runner, result, _report: writes.append(result.status),
    )
    monkeypatch.setattr(
        runner_module,
        "emit_scenario_completed",
        lambda *_args, **_kwargs: terminal_events.append("emitted"),
    )
    runner = ScenarioRunner(
        _settings(tmp_path),
        _registry(block),
        result_reporter=reporter,
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner.run(_scenario()))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert exited.is_set()
    assert cleanup_flags == [True]
    assert reporter.statuses == []
    assert writes == []
    assert terminal_events == []


def test_hung_runtime_cleanup_is_cancelled_at_its_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_finished = asyncio.Event()
    reporter = RecordingReporter()

    async def execute(_context: RunContext, _params: ProbeParams) -> dict[str, bool]:
        return {"success": True}

    async def hang(_services: ExecutionServices, *, failed: bool) -> None:
        assert failed is False
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_finished.set()

    monkeypatch.setattr(ExecutionServices, "close", hang)
    runner = ScenarioRunner(
        _settings(tmp_path, cleanup_timeout_seconds=0.01),
        _registry(execute),
        result_reporter=reporter,
    )

    with pytest.raises(ActivityExecutionError, match="cleanup exceeded"):
        asyncio.run(runner.run(_scenario()))

    assert cleanup_finished.is_set()
    assert reporter.statuses == ["failed"]
