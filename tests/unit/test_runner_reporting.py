"""Runner integration tests for isolated, shared result reporting."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

from plantain.config import Settings
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runner import ScenarioRunner
from plantain.engine.runtime import RunContext
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.persistence import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE
from plantain.reporting import PublicationResult
from plantain.reporting.allure import (
    MAX_ALLURE_INTEGRATIONS,
    write_allure_result,
)
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

T = TypeVar("T")
EXPECTED_IMMUTABLE_RESULTS = 2


@pytest.fixture(autouse=True)
def _run_blocking_callbacks_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep runner-boundary tests deterministic on UNC-mounted workspaces."""

    async def inline(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)


class ProbeParams(BaseModel):
    id: str


class RecordingReporter:
    provider = "recording"

    def __init__(self, *, fail: bool = False, fail_close: bool = False) -> None:
        self.fail = fail
        self.fail_close = fail_close
        self.validated: list[str] = []
        self.published: list[Mapping[str, Any]] = []
        self.close_count = 0

    def validate_scenario(self, scenario: ScenarioDefinition) -> None:
        self.validated.append(scenario.scenario)

    async def publish(
        self,
        _scenario: ScenarioDefinition,
        *,
        status: str,
        duration_ms: int,
        report: Mapping[str, Any],
        secrets: SecretRegistry,
    ) -> PublicationResult:
        del duration_ms, secrets
        self.published.append(report)
        if self.fail:
            raise RuntimeError("synthetic reporter detail must remain isolated")
        return PublicationResult(provider=self.provider, status=f"recorded_{status}")

    async def close(self) -> None:
        self.close_count += 1
        if self.fail_close:
            raise RuntimeError("synthetic reporter cleanup detail must remain isolated")


def _settings(tmp_path: Path) -> Settings:
    return cast(
        "Settings",
        SimpleNamespace(
            environment="test",
            sensitive_key_names=("private_value",),
            allow_private_networks=False,
            allowed_hosts=("example.test",),
            scenario_timeout_seconds=2.0,
            step_timeout_seconds=1.0,
            output_dir=tmp_path / "output",
            allure_results_enabled=False,
        ),
    )


def _registry() -> ActivityRegistry:
    async def probe(context: RunContext, _params: ProbeParams) -> dict[str, bool]:
        context.add_operation(
            domain="test",
            phase="probe",
            operation_type="read",
            target="synthetic-resource",
            status="passed",
            duration_ms=1,
            operation_input={
                "username": "standard_user",
                "password": "must-not-persist",
            },
            operation_actual={"httpStatus": 200},
        )
        return {"success": True}

    registry = ActivityRegistry()
    registry.register(
        "probe",
        ProbeParams,
        probe,
        description="Exercise reporting after a passing step.",
    )
    return registry


def _scenario(name: str) -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario=name,
        jira_ticket="QA-42",
        test_case_key="QA-T42",
        test_run_key="QA-R7",
        tags=["reporting"],
        steps=[StepDefinition(activity="probe", params={"id": "probe"})],
    )


def test_runner_persists_metadata_and_publication_outcome(tmp_path: Path) -> None:
    reporter = RecordingReporter()
    settings = _settings(tmp_path)
    settings.allure_results_enabled = True
    runner = ScenarioRunner(
        settings,
        _registry(),
        result_reporter=reporter,
    )

    result = asyncio.run(runner.run(_scenario("Reporting integration")))

    assert result.status == "passed"
    assert result.jira_ticket == "QA-42"
    assert result.test_case_key == "QA-T42"
    assert result.test_run_key == "QA-R7"
    assert result.integrations["recording"]["status"] == "recorded_passed"
    assert reporter.validated == ["Reporting integration"]
    assert len(reporter.published) == 1
    assert reporter.close_count == 0

    report_path = (
        tmp_path / f"output/results/reporting_integration/{result.correlation_id}.result.json"
    )
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["jira_ticket"] == "QA-42"
    assert persisted["test_case_key"] == "QA-T42"
    assert persisted["tags"] == ["reporting"]
    assert persisted["operations"][0]["operation_input"] == {
        "username": "standard_user",
        "password": REDACTED,
    }
    assert reporter.published[0]["operations"] == persisted["operations"]
    assert persisted["integrations"]["recording"]["status"] == "recorded_passed"
    assert persisted["integrations"]["allure"]["status"] == "written"
    assert "allure" not in reporter.published[0]["integrations"]

    allure_paths = list((tmp_path / "output/allure").glob("*-result.json"))
    assert len(allure_paths) == 1
    allure_result = json.loads(allure_paths[0].read_text(encoding="utf-8"))
    assert allure_result["status"] == "passed"
    assert allure_result["labels"][-1] == {"name": "tag", "value": "reporting"}
    if os.name != "nt":
        directories = (report_path.parent, allure_paths[0].parent)
        files = (report_path, allure_paths[0])
        assert all(
            stat.S_IMODE(path.stat().st_mode) == PRIVATE_DIRECTORY_MODE for path in directories
        )
        assert all(stat.S_IMODE(path.stat().st_mode) == PRIVATE_FILE_MODE for path in files)
    integration_parameters = {
        item["name"]: json.loads(item["value"])
        for item in allure_result["parameters"]
        if item["name"].startswith("integration.")
    }
    assert integration_parameters["integration.recording"]["status"] == "recorded_passed"
    assert integration_parameters["integration.allure"]["status"] == "written"
    operation_name = allure_result["steps"][0]["name"]
    assert "TEST read" in operation_name
    assert "target=synthetic-resource" in operation_name
    assert "standard_user" in operation_name
    assert 'actual={"httpStatus":200}' in operation_name
    assert REDACTED in operation_name
    assert "must-not-persist" not in json.dumps(allure_result)


def test_runner_preserves_each_execution_of_the_same_scenario(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    scenario = _scenario("Repeated scenario")

    first = asyncio.run(ScenarioRunner(settings, _registry()).run(scenario))
    second = asyncio.run(ScenarioRunner(settings, _registry()).run(scenario))

    reports = sorted((settings.output_dir / "results/repeated_scenario").glob("*.result.json"))
    assert first.correlation_id != second.correlation_id
    assert len(reports) == EXPECTED_IMMUTABLE_RESULTS
    assert {path.name.removesuffix(".result.json") for path in reports} == {
        first.correlation_id,
        second.correlation_id,
    }
    assert {json.loads(path.read_text(encoding="utf-8"))["correlation_id"] for path in reports} == {
        first.correlation_id,
        second.correlation_id,
    }


@pytest.mark.parametrize(
    ("fail_close", "cleanup_status", "cleanup_failure_stage"),
    [
        (False, "closed", None),
        (True, "failed", "cleanup"),
    ],
)
def test_owned_reporter_cleanup_is_in_final_views(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_close: bool,
    cleanup_status: str,
    cleanup_failure_stage: str | None,
) -> None:
    reporter = RecordingReporter(fail_close=fail_close)
    monkeypatch.setattr(
        "plantain.engine.reporting.ZephyrReporter",
        lambda _settings, **_kwargs: reporter,
    )
    settings = _settings(tmp_path)
    settings.allure_results_enabled = True
    result = asyncio.run(ScenarioRunner(settings, _registry()).run(_scenario("Owned cleanup")))

    assert result.status == "passed"
    assert reporter.close_count == 1
    integration = result.integrations["recording"]
    assert integration["cleanup_status"] == cleanup_status
    assert integration["cleanup_failure_stage"] == cleanup_failure_stage

    native_path = tmp_path / f"output/results/owned_cleanup/{result.correlation_id}.result.json"
    native = json.loads(native_path.read_text(encoding="utf-8"))
    assert native["integrations"]["recording"] == integration
    allure_path = next((tmp_path / "output/allure").glob("*-result.json"))
    allure = json.loads(allure_path.read_text(encoding="utf-8"))
    projected = next(
        json.loads(item["value"])
        for item in allure["parameters"]
        if item["name"] == "integration.recording"
    )
    assert projected["cleanup_status"] == cleanup_status
    if cleanup_failure_stage is None:
        assert "cleanup_failure_stage" not in projected
    else:
        assert projected["cleanup_failure_stage"] == cleanup_failure_stage


def test_allure_integration_parameters_are_bounded(tmp_path: Path) -> None:
    integrations = {
        f"provider-{index}": {
            "status": "failed",
            "failure_stage": "synthetic",
        }
        for index in range(MAX_ALLURE_INTEGRATIONS + 5)
    }
    target = write_allure_result(
        tmp_path,
        {
            "scenario": "Bounded integrations",
            "status": "passed",
            "integrations": integrations,
        },
        environment="test",
        secrets=SecretRegistry(sensitive_keys=()),
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    parameters = [item for item in payload["parameters"] if item["name"].startswith("integration.")]
    assert len(parameters) == MAX_ALLURE_INTEGRATIONS


def test_allure_exception_never_changes_a_passing_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_writer(*_args: Any, **_kwargs: Any) -> Path:
        raise OSError("synthetic filesystem detail must remain isolated")

    settings = _settings(tmp_path)
    settings.allure_results_enabled = True
    monkeypatch.setattr("plantain.engine.runner.write_allure_result", fail_writer)
    runner = ScenarioRunner(
        settings,
        _registry(),
        result_reporter=RecordingReporter(),
    )

    result = asyncio.run(runner.run(_scenario("Allure failure isolation")))

    assert result.status == "passed"
    assert result.integrations["allure"] == {
        "provider": "allure",
        "status": "failed",
        "result_file": None,
        "failure_stage": "allure_persistence",
    }
    report_path = (
        tmp_path / f"output/results/allure_failure_isolation/{result.correlation_id}.result.json"
    )
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["integrations"]["allure"] == result.integrations["allure"]


def test_reporter_exception_never_changes_a_passing_test(tmp_path: Path) -> None:
    reporter = RecordingReporter(fail=True)
    runner = ScenarioRunner(
        _settings(tmp_path),
        _registry(),
        result_reporter=reporter,
    )

    result = asyncio.run(runner.run(_scenario("Reporting failure isolation")))

    assert result.status == "passed"
    assert result.integrations["recording"] == {
        "provider": "recording",
        "status": "failed",
        "http_status": None,
        "execution_id": None,
        "failure_stage": "framework",
        "delivery_state": "ambiguous",
        "retry_status": "manual_reconciliation",
        "outbox_id": None,
        "attachment_status": "not_attempted",
        "attachment_http_status": None,
    }


def test_concurrent_batch_shares_one_owned_reporter_and_closes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(
        "plantain.engine.reporting.ZephyrReporter",
        lambda _settings, **_kwargs: reporter,
    )
    runner = ScenarioRunner(_settings(tmp_path), _registry())
    scenarios = [_scenario(f"Concurrent report {index}") for index in range(4)]

    results = asyncio.run(runner.run_many(scenarios, concurrency=2))

    assert all(not isinstance(result, BaseException) for result in results)
    assert len(reporter.published) == len(scenarios)
    assert reporter.close_count == 1
