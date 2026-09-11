"""Runner-level global scenario admission tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from plantain.config import Settings
from plantain.engine.progress import RunProgressEvent
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runner import ScenarioResult, ScenarioRunner
from plantain.engine.runtime import RunContext
from plantain.models.scenario import ScenarioDefinition

EXPECTED_SCENARIO_PEAK = 2


def test_run_many_rejects_concurrency_above_configured_ceiling(tmp_path: Path) -> None:
    settings = replace(
        Settings.from_env(tmp_path),
        max_scenario_concurrency=1,
    )
    runner = ScenarioRunner(settings, ActivityRegistry())

    with pytest.raises(ValueError, match="PLANTAIN_MAX_SCENARIO_CONCURRENCY"):
        asyncio.run(runner.run_many([], concurrency=2))


def test_run_many_rejects_reentrancy_and_oversized_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ScenarioRunner(Settings.from_env(tmp_path), ActivityRegistry())
    runner._batch_active = True
    with pytest.raises(RuntimeError, match="not reentrant"):
        asyncio.run(runner.run_many([]))

    runner._batch_active = False
    monkeypatch.setattr("plantain.engine.runner.MAX_DISCOVERED_SCENARIO_FILES", 1)
    scenarios = [
        ScenarioDefinition.model_construct(scenario=f"scenario_{index}", steps=[])
        for index in range(2)
    ]
    with pytest.raises(ValueError, match="batch must not exceed 1"):
        asyncio.run(runner.run_many(scenarios))


def test_direct_runs_share_scenario_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        Settings.from_env(tmp_path),
        max_scenario_concurrency=EXPECTED_SCENARIO_PEAK,
    )
    settings.ensure_runtime_directories()
    events: list[RunProgressEvent] = []
    runner = ScenarioRunner(
        settings,
        ActivityRegistry(),
        progress_observer=events.append,
    )
    active = 0
    peak = 0
    admitted = asyncio.Event()
    release = asyncio.Event()

    async def execute(
        _runner: ScenarioRunner,
        _context: RunContext,
        _result: ScenarioResult,
    ) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == EXPECTED_SCENARIO_PEAK:
            admitted.set()
        try:
            await release.wait()
        finally:
            active -= 1

    monkeypatch.setattr(ScenarioRunner, "_execute_scenario", execute)

    async def exercise() -> list[ScenarioResult]:
        tasks = [
            asyncio.create_task(
                runner.run(
                    ScenarioDefinition.model_construct(
                        scenario=f"admission_{index}",
                        steps=[],
                    )
                )
            )
            for index in range(3)
        ]
        await asyncio.wait_for(admitted.wait(), timeout=1)
        await asyncio.sleep(0)
        assert peak == EXPECTED_SCENARIO_PEAK
        release.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(exercise())

    assert all(result.status == "passed" for result in results)
    scenario_events = [event for event in events if event.kind == "scenario"]
    assert {event.status for event in scenario_events} == {"running", "passed"}
    assert len({event.correlation_id for event in scenario_events}) == len(results)
