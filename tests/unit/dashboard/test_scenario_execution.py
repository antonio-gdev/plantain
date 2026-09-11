"""Direct dashboard execution remains isolated, bounded, and browser-safe."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.dashboard import scenario_execution
from plantain.dashboard.scenario_execution import (
    DashboardRunError,
    DashboardRunOutcome,
    dashboard_run_capacity,
    run_dashboard_scenario,
)
from plantain.engine.admission import ResourceKind
from plantain.engine.progress import RunProgressEvent, RunProgressObserver
from plantain.engine.runner import ScenarioResult, StepResult
from plantain.errors import ActivityExecutionError
from plantain.models.scenario import ScenarioDefinition, StepDefinition

SCENARIO_ID = "a" * 64
SECOND_SCENARIO_ID = "b" * 64
EXPECTED_DURATION_MS = 37
EXPECTED_SCENARIO_CAPACITY = 3
SENSITIVE_FAILURE_DETAIL = "synthetic-private-runtime-detail"


@dataclass(slots=True)
class _RunnerProbe:
    admission: object
    result: ScenarioResult
    run_error: BaseException | None = None
    validation_error: Exception | None = None
    reporting_calls: int = 0
    run_calls: int = 0
    validated: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    progress_observer: RunProgressObserver | None = None


class _Registry:
    def __init__(self, probe: _RunnerProbe) -> None:
        self._probe = probe

    def validate(self, activity: str, params: dict[str, Any]) -> None:
        self._probe.validated.append((activity, params))
        if self._probe.validation_error is not None:
            raise self._probe.validation_error


class _Runner:
    def __init__(self, probe: _RunnerProbe) -> None:
        self._probe = probe
        self.registry = _Registry(probe)

    def validate_reporting(self, _scenario: ScenarioDefinition) -> None:
        self._probe.reporting_calls += 1

    def validate_scenario(self, scenario: ScenarioDefinition) -> None:
        self.validate_reporting(scenario)
        for step in scenario.steps:
            self.registry.validate(step.activity, step.params)

    async def run(self, _scenario: ScenarioDefinition) -> ScenarioResult:
        self._probe.run_calls += 1
        if self._probe.progress_observer is not None:
            self._probe.progress_observer(
                RunProgressEvent(
                    correlation_id=SCENARIO_ID[:32],
                    kind="scenario",
                    status="running",
                    completed_steps=0,
                    total_steps=1,
                )
            )
        if self._probe.run_error is not None:
            raise self._probe.run_error
        return self._probe.result


@dataclass(frozen=True, slots=True)
class _SettingsRecord:
    allow_db_mutations: bool
    ensured: list[str]

    def ensure_runtime_directories(self) -> None:
        self.ensured.append("ensured")


def _scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario="Checkout journey",
        steps=[
            StepDefinition(
                activity="sendRequest",
                params={"id": "request"},
            )
        ],
    )


def _result() -> ScenarioResult:
    return ScenarioResult(
        scenario="Checkout journey",
        status="passed",
        duration_ms=EXPECTED_DURATION_MS,
        correlation_id="safe-correlation-id",
        steps=[
            StepResult(
                activity="sendRequest",
                step_id="request",
                status="passed",
                duration_ms=EXPECTED_DURATION_MS,
            )
        ],
    )


def _install_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe: _RunnerProbe,
) -> None:
    settings = SimpleNamespace(
        project_root=tmp_path,
        sensitive_key_names=(),
    )
    runtime = SimpleNamespace(admission=probe.admission)
    scenario = _scenario()
    scenario_path = tmp_path / "scenarios/checkout.yaml"

    monkeypatch.setattr(scenario_execution, "_runtime_settings", lambda _root: settings)
    monkeypatch.setattr(
        scenario_execution,
        "dashboard_reporting_runtime",
        lambda configured: SimpleNamespace(settings=configured, environment={}),
    )
    monkeypatch.setattr(scenario_execution, "_runtime_for", lambda _settings: runtime)
    monkeypatch.setattr(
        scenario_execution,
        "resolve_scenario_path",
        lambda _root, _scenario_id: scenario_path,
    )
    monkeypatch.setattr(
        scenario_execution,
        "load_scenario",
        lambda _path, _settings: scenario,
    )

    def runner_factory(
        _settings: Any,
        *,
        admission: Any,
        progress_observer: RunProgressObserver | None,
        reporting_environ: Any,
    ) -> _Runner:
        assert admission is probe.admission
        assert reporting_environ == {}
        probe.progress_observer = progress_observer
        return _Runner(probe)

    monkeypatch.setattr(scenario_execution, "ScenarioRunner", runner_factory)


def test_dashboard_capacity_uses_the_shared_runtime_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[ResourceKind] = []

    def limit(resource: ResourceKind) -> int:
        observed.append(resource)
        return EXPECTED_SCENARIO_CAPACITY

    settings = object()
    runtime = SimpleNamespace(admission=SimpleNamespace(limit=limit))
    monkeypatch.setattr(scenario_execution, "_runtime_settings", lambda _root: settings)
    monkeypatch.setattr(scenario_execution, "_runtime_for", lambda _settings: runtime)

    assert dashboard_run_capacity(tmp_path) == EXPECTED_SCENARIO_CAPACITY
    assert observed == [ResourceKind.SCENARIO]


def test_run_returns_only_compact_browser_safe_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _RunnerProbe(admission=object(), result=_result())
    _install_execution(monkeypatch, tmp_path, probe)

    outcome = asyncio.run(run_dashboard_scenario(tmp_path, SCENARIO_ID))

    assert outcome == DashboardRunOutcome(
        scenario_id=SCENARIO_ID,
        name="Checkout journey",
        status="passed",
        duration_ms=EXPECTED_DURATION_MS,
        completed_steps=1,
        correlation_id="safe-correlation-id",
        message="1 step completed.",
    )
    assert probe.reporting_calls == 1
    assert probe.run_calls == 1
    assert probe.validated == [("sendRequest", {"id": "request"})]


def test_run_forwards_value_free_progress_observer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _RunnerProbe(admission=object(), result=_result())
    _install_execution(monkeypatch, tmp_path, probe)
    events: list[RunProgressEvent] = []

    asyncio.run(
        run_dashboard_scenario(
            tmp_path,
            SCENARIO_ID,
            progress_observer=events.append,
        )
    )

    assert len(events) == 1
    assert events[0].kind == "scenario"
    assert not hasattr(events[0], "operation_actual")


def test_expected_failure_is_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _RunnerProbe(
        admission=object(),
        result=_result(),
        run_error=ActivityExecutionError(SENSITIVE_FAILURE_DETAIL),
    )
    _install_execution(monkeypatch, tmp_path, probe)

    outcome = asyncio.run(run_dashboard_scenario(tmp_path, SCENARIO_ID))

    assert outcome.status == "failed"
    assert outcome.completed_steps == 0
    assert outcome.correlation_id == ""
    assert outcome.duration_ms >= 0
    assert SENSITIVE_FAILURE_DETAIL not in repr(outcome)


def test_preflight_failure_prevents_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _RunnerProbe(
        admission=object(),
        result=_result(),
        validation_error=ActivityExecutionError(SENSITIVE_FAILURE_DETAIL),
    )
    _install_execution(monkeypatch, tmp_path, probe)

    with pytest.raises(
        DashboardRunError,
        match="must be reviewed before it can run",
    ) as captured:
        asyncio.run(run_dashboard_scenario(tmp_path, SCENARIO_ID))

    assert probe.run_calls == 0
    assert SENSITIVE_FAILURE_DETAIL not in str(captured.value)


def test_stale_selection_fails_before_runner_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _RunnerProbe(admission=object(), result=_result())
    _install_execution(monkeypatch, tmp_path, probe)

    def reject_selection(_root: Path, _scenario_id: str) -> Path:
        raise scenario_execution.ScenarioCatalogError(SENSITIVE_FAILURE_DETAIL)

    monkeypatch.setattr(
        scenario_execution,
        "resolve_scenario_path",
        reject_selection,
    )
    monkeypatch.setattr(
        scenario_execution,
        "ScenarioRunner",
        lambda *_args, **_kwargs: pytest.fail("runner must not be constructed"),
    )

    with pytest.raises(DashboardRunError, match="changed and must be reviewed"):
        asyncio.run(run_dashboard_scenario(tmp_path, SCENARIO_ID))


def test_runner_construction_failure_is_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _RunnerProbe(admission=object(), result=_result())
    _install_execution(monkeypatch, tmp_path, probe)

    def reject_runner(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(SENSITIVE_FAILURE_DETAIL)

    monkeypatch.setattr(scenario_execution, "ScenarioRunner", reject_runner)

    with pytest.raises(
        DashboardRunError,
        match="could not be prepared safely",
    ) as captured:
        asyncio.run(run_dashboard_scenario(tmp_path, SCENARIO_ID))
    assert SENSITIVE_FAILURE_DETAIL not in str(captured.value)


def test_unexpected_failure_is_translated_and_cancellation_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _RunnerProbe(
        admission=object(),
        result=_result(),
        run_error=RuntimeError(SENSITIVE_FAILURE_DETAIL),
    )
    _install_execution(monkeypatch, tmp_path, probe)

    with pytest.raises(
        DashboardRunError,
        match="could not be completed safely",
    ) as captured:
        asyncio.run(run_dashboard_scenario(tmp_path, SCENARIO_ID))
    assert SENSITIVE_FAILURE_DETAIL not in str(captured.value)

    probe.run_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_dashboard_scenario(tmp_path, SCENARIO_ID))


def test_runtime_settings_disable_human_database_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _SettingsRecord(allow_db_mutations=True, ensured=[])
    monkeypatch.setattr(
        scenario_execution.Settings,
        "from_env",
        lambda _root: cast("Any", record),
    )

    settings = scenario_execution._runtime_settings(tmp_path)

    assert settings.allow_db_mutations is False
    assert record.ensured == ["ensured"]


def _identity_settings(root: Path, output_name: str) -> Any:
    return SimpleNamespace(
        project_root=root,
        output_dir=root / output_name,
        log_level="INFO",
        sensitive_key_names=(),
        max_scenario_concurrency=4,
        max_browser_sessions=2,
        max_api_requests=16,
        max_database_operations=8,
        max_worker_threads=8,
        max_worker_processes=2,
    )


def test_runtime_owner_reuses_one_admission_and_rejects_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = scenario_execution._RuntimeOwner()
    admission = object()
    initializations: list[object] = []

    def initialize(settings: Any, identity: Any) -> Any:
        initializations.append(settings)
        return scenario_execution._ExecutionRuntime(
            identity=identity,
            admission=cast("Any", admission),
        )

    monkeypatch.setattr(scenario_execution, "_initialize_runtime", initialize)
    settings = _identity_settings(tmp_path, "output")

    first = owner.get_or_create(settings)
    second = owner.get_or_create(settings)

    assert first is second
    assert first.admission is admission
    assert initializations == [settings]
    with pytest.raises(DashboardRunError, match="restart Plantain"):
        owner.get_or_create(_identity_settings(tmp_path, "alternate-output"))


def _dashboard_outcome(scenario_id: str) -> DashboardRunOutcome:
    return DashboardRunOutcome(
        scenario_id=scenario_id,
        name="Concurrent test",
        status="passed",
        duration_ms=EXPECTED_DURATION_MS,
        completed_steps=1,
        correlation_id=f"run-{scenario_id[:8]}",
        message="1 step completed.",
    )


def test_coordinator_runs_concurrently_and_cancels_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        coordinator = scenario_execution._RunCoordinator()
        started = {
            SCENARIO_ID: asyncio.Event(),
            SECOND_SCENARIO_ID: asyncio.Event(),
        }
        release = {
            SCENARIO_ID: asyncio.Event(),
            SECOND_SCENARIO_ID: asyncio.Event(),
        }
        cancelled: list[str] = []

        async def run(
            _root: Path,
            scenario_id: str,
            *,
            progress_observer: RunProgressObserver | None,
        ) -> DashboardRunOutcome:
            started[scenario_id].set()
            if progress_observer is not None:
                progress_observer(
                    RunProgressEvent(
                        correlation_id=scenario_id[:32],
                        kind="scenario",
                        status="running",
                        completed_steps=0,
                        total_steps=1,
                    )
                )
            try:
                await release[scenario_id].wait()
            except asyncio.CancelledError:
                cancelled.append(scenario_id)
                raise
            return _dashboard_outcome(scenario_id)

        monkeypatch.setattr(scenario_execution, "run_dashboard_scenario", run)
        first = coordinator.start(tmp_path, SCENARIO_ID)
        second = coordinator.start(tmp_path, SECOND_SCENARIO_ID)
        await asyncio.gather(*(event.wait() for event in started.values()))

        assert first.job_id != second.job_id
        assert scenario_execution.is_valid_dashboard_job_id(first.job_id)
        assert scenario_execution.is_valid_dashboard_job_id(second.job_id)
        assert "_task" not in repr(first)
        first_progress, second_progress = await asyncio.gather(
            scenario_execution.wait_dashboard_run_progress(first),
            scenario_execution.wait_dashboard_run_progress(second),
        )
        assert first_progress.correlation_id == SCENARIO_ID[:32]
        assert second_progress.correlation_id == SECOND_SCENARIO_ID[:32]
        assert coordinator.cancel(first.job_id)
        with pytest.raises(asyncio.CancelledError):
            await scenario_execution.wait_dashboard_run(first)

        release[SECOND_SCENARIO_ID].set()
        assert await scenario_execution.wait_dashboard_run(second) == (
            _dashboard_outcome(SECOND_SCENARIO_ID)
        )
        await asyncio.sleep(0)

        assert cancelled == [SCENARIO_ID]
        assert not coordinator.cancel(first.job_id)
        assert not coordinator.cancel(second.job_id)
        assert not coordinator.cancel("invalid")

    asyncio.run(exercise())


def test_monitor_projection_failure_does_not_cancel_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        coordinator = scenario_execution._RunCoordinator()
        release = asyncio.Event()
        updates: list[scenario_execution.DashboardRunProgress] = []

        async def run(
            _root: Path,
            scenario_id: str,
            *,
            progress_observer: RunProgressObserver | None,
        ) -> DashboardRunOutcome:
            assert progress_observer is not None
            progress_observer(
                RunProgressEvent(
                    correlation_id=scenario_id[:32],
                    kind="scenario",
                    status="running",
                    total_steps=1,
                )
            )
            await release.wait()
            return _dashboard_outcome(scenario_id)

        async def reject_projection(
            progress: scenario_execution.DashboardRunProgress,
        ) -> None:
            updates.append(progress)
            release.set()
            raise RuntimeError(SENSITIVE_FAILURE_DETAIL)

        monkeypatch.setattr(scenario_execution, "run_dashboard_scenario", run)
        handle = coordinator.start(tmp_path, SCENARIO_ID)
        outcome = await scenario_execution.monitor_dashboard_run(
            handle,
            reject_projection,
        )

        assert outcome == _dashboard_outcome(SCENARIO_ID)
        assert len(updates) == 1
        assert SENSITIVE_FAILURE_DETAIL not in repr(updates)

    asyncio.run(exercise())


def test_public_start_rejects_malformed_identifier_before_task_allocation(
    tmp_path: Path,
) -> None:
    with pytest.raises(DashboardRunError, match="identifier is invalid"):
        scenario_execution.start_dashboard_run(tmp_path, "invalid")
    with pytest.raises(DashboardRunError, match="identifier is invalid"):
        scenario_execution.start_dashboard_run(tmp_path, cast("Any", None))
