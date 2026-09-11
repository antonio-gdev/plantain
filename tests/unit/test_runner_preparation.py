"""Internal activity preparation executes inside the scenario deadline."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

from plantain.config import Settings
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runner import ScenarioRunner
from plantain.engine.runtime import RunContext
from plantain.errors import ActivityExecutionError
from plantain.models.scenario import ScenarioDefinition, StepDefinition

ResultT = TypeVar("ResultT")


@pytest.fixture(autouse=True)
def _run_threads_inline_on_mounted_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_inline(
        function: Callable[..., ResultT],
        *args: Any,
        **kwargs: Any,
    ) -> ResultT:
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)


class PreparedParams(BaseModel):
    id: str


def _settings(
    tmp_path: Path,
    *,
    scenario_timeout_seconds: float = 1.0,
) -> Settings:
    return cast(
        "Settings",
        SimpleNamespace(
            sensitive_key_names=(),
            environment="test",
            allow_private_networks=False,
            allowed_hosts=("example.test",),
            scenario_timeout_seconds=scenario_timeout_seconds,
            step_timeout_seconds=0.01,
            cleanup_timeout_seconds=0.1,
            output_dir=tmp_path / "output",
        ),
    )


def test_preparer_runs_once_before_repeated_activity_steps(tmp_path: Path) -> None:
    events: list[str] = []

    async def prepare(_context: RunContext) -> None:
        await asyncio.sleep(0.02)
        events.append("prepare")

    async def execute(_context: RunContext, params: PreparedParams) -> dict[str, str]:
        events.append(params.id)
        return {"id": params.id}

    registry = ActivityRegistry()
    registry.register(
        "preparedActivity",
        PreparedParams,
        execute,
        description="Prepared runner test activity",
        preparer=prepare,
    )
    scenario = ScenarioDefinition(
        scenario="Prepared activity",
        steps=[
            StepDefinition(activity="preparedActivity", params={"id": "first"}),
            StepDefinition(activity="preparedActivity", params={"id": "second"}),
        ],
    )

    result = asyncio.run(ScenarioRunner(_settings(tmp_path), registry).run(scenario))

    assert result.status == "passed"
    assert events == ["prepare", "first", "second"]


def test_preparer_is_cancelled_by_scenario_deadline(tmp_path: Path) -> None:
    events: list[str] = []

    async def prepare(_context: RunContext) -> None:
        events.append("prepare-started")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            events.append("prepare-cancelled")
            raise

    async def execute(_context: RunContext, _params: PreparedParams) -> None:
        events.append("execute")

    registry = ActivityRegistry()
    registry.register(
        "preparedActivity",
        PreparedParams,
        execute,
        description="Scenario-bounded preparer.",
        preparer=prepare,
    )
    scenario = ScenarioDefinition(
        scenario="Preparation timeout",
        steps=[StepDefinition(activity="preparedActivity", params={"id": "probe"})],
    )

    with pytest.raises(ActivityExecutionError, match="TimeoutError"):
        asyncio.run(
            ScenarioRunner(
                _settings(tmp_path, scenario_timeout_seconds=0.01),
                registry,
            ).run(scenario)
        )

    assert events == ["prepare-started", "prepare-cancelled"]
    assert "execute" not in events
