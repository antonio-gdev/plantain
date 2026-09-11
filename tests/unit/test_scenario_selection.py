"""Safe recursive selection and bounded concurrent execution tests."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from plantain import cli
from plantain.config import Settings
from plantain.engine.loader import discover_scenario_paths, load_scenario
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runner import ScenarioResult, ScenarioRunner
from plantain.engine.selection import ScenarioTagFilter, select_scenarios
from plantain.errors import AtomicPersistenceError, PlantainError, ScenarioLoadError
from plantain.models.scenario import (
    MAX_SCENARIO_METADATA_ENTRIES,
    MAX_SCENARIO_STEPS,
    MAX_STEP_PARAMETERS,
    ScenarioDefinition,
    StepDefinition,
)

EXPECTED_CONCURRENCY = 2
SCENARIO_COUNT = 4
CLI_FAILURE_STATUS = 2


def _settings(tmp_path: Path) -> Settings:
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    return cast(
        "Settings",
        SimpleNamespace(
            project_root=tmp_path,
            scenarios_dir=scenarios_dir,
            yaml_max_bytes=2_000_000,
            yaml_max_nodes=50_000,
            yaml_max_depth=100,
        ),
    )


def _write_scenario(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''scenario: "{name}"
steps:
  - capturePageSnapshot:
      id: page
      url: https://example.test
''',
        encoding="utf-8",
    )


def _relative(paths: list[Path], settings: Settings) -> list[str]:
    return [path.relative_to(settings.scenarios_dir).as_posix() for path in paths]


def test_discovers_nested_directories_deterministically_without_duplicates(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _write_scenario(settings.scenarios_dir / "team_b/nested/z.yml", "Zed")
    _write_scenario(settings.scenarios_dir / "team_a/a.yaml", "Alpha")
    (settings.scenarios_dir / "team_b/notes.txt").write_text("ignored", encoding="utf-8")

    paths = discover_scenario_paths(
        [Path("team_b"), Path("scenarios/team_a"), Path("team_b/nested/z.yml")],
        settings,
    )

    assert _relative(paths, settings) == ["team_a/a.yaml", "team_b/nested/z.yml"]


def test_scenario_discovery_bounds_files_and_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    _write_scenario(settings.scenarios_dir / "one.yaml", "One")
    _write_scenario(settings.scenarios_dir / "two.yaml", "Two")
    monkeypatch.setattr("plantain.engine.loader.MAX_DISCOVERED_SCENARIO_FILES", 1)

    with pytest.raises(ScenarioLoadError, match="maximum of 1 files"):
        discover_scenario_paths([Path()], settings)


def test_rejects_empty_scenario_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.scenarios_dir / "empty").mkdir()

    with pytest.raises(ScenarioLoadError, match="contains no YAML files"):
        discover_scenario_paths([Path("empty")], settings)


def test_dashboard_discovery_allows_empty_scenario_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.scenarios_dir / "empty").mkdir()

    assert (
        discover_scenario_paths(
            [Path("empty")],
            settings,
            allow_empty=True,
        )
        == []
    )


def test_rejects_input_outside_scenario_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outside = tmp_path / "outside.yaml"
    _write_scenario(outside, "Outside")

    with pytest.raises(ScenarioLoadError, match="must stay inside"):
        discover_scenario_paths([outside], settings)


def test_rejects_symlink_in_discovered_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    team = settings.scenarios_dir / "team"
    team.mkdir()
    outside = tmp_path / "outside.yaml"
    _write_scenario(outside, "Outside")
    link = team / "linked.yaml"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("Filesystem does not support symbolic links")

    with pytest.raises(ScenarioLoadError, match="cannot contain symlinks"):
        discover_scenario_paths([Path("team")], settings)


def test_scenario_loader_uses_bounded_no_follow_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    target = settings.scenarios_dir / "scenario.yaml"
    _write_scenario(target, "Scenario")

    def reject_read(_path: Path) -> Any:
        raise AtomicPersistenceError("synthetic filesystem detail")

    monkeypatch.setattr("plantain.engine.loader.open_binary_read_no_follow", reject_read)
    with pytest.raises(ScenarioLoadError, match="could not be read safely") as captured:
        load_scenario(target, settings)
    assert "synthetic filesystem detail" not in str(captured.value)


def test_scenario_loader_enforces_composition_bounds(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    target = settings.scenarios_dir / "scenario.yaml"
    _write_scenario(target, "Scenario")
    settings.yaml_max_nodes = 2
    with pytest.raises(ScenarioLoadError, match="maximum of 2 nodes"):
        load_scenario(target, settings)

    settings.yaml_max_nodes = 50_000
    settings.yaml_max_depth = 1
    with pytest.raises(ScenarioLoadError, match="maximum depth of 1"):
        load_scenario(target, settings)


def test_cli_validate_accepts_nested_team_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    _write_scenario(settings.scenarios_dir / "team/smoke.yaml", "Smoke")
    args = argparse.Namespace(scenarios=[Path("team")])

    status = cli._validate(args, settings)

    assert status == 0
    assert "VALID" in capsys.readouterr().out


def test_static_validation_skips_runtime_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_side_effect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("static validation must remain side-effect free")

    monkeypatch.setattr(cli, "load_dotenv", reject_side_effect)
    monkeypatch.setattr(cli, "configure_logging", reject_side_effect)
    monkeypatch.setattr(Settings, "ensure_runtime_directories", reject_side_effect)
    args = argparse.Namespace(
        project_root=tmp_path,
        no_dotenv=False,
        command="validate",
    )

    assert cli._settings(args).project_root == tmp_path


def test_activities_skips_settings_and_unexpected_failures_are_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_value = "/private/project/customer-record"
    parser = SimpleNamespace(
        parse_args=lambda: argparse.Namespace(command="activities"),
    )

    def fail_activity() -> int:
        raise RuntimeError(sensitive_value)

    monkeypatch.setattr(cli, "_parser", lambda: parser)
    monkeypatch.setattr(cli, "_settings", lambda _args: pytest.fail("settings were loaded"))
    monkeypatch.setattr(cli, "_activities", fail_activity)

    with pytest.raises(SystemExit) as captured:
        cli.main()

    assert captured.value.code == CLI_FAILURE_STATUS
    assert sensitive_value not in capsys.readouterr().err


def test_scenario_models_bound_retained_collections() -> None:
    parameters = {str(index): index for index in range(MAX_STEP_PARAMETERS + 1)}
    with pytest.raises(ValidationError, match="at most 1000 items"):
        StepDefinition(activity="activity", params=parameters)
    step = StepDefinition(activity="activity", params={})
    with pytest.raises(ValidationError, match="at most 1000 items"):
        ScenarioDefinition(scenario="steps", steps=[step] * (MAX_SCENARIO_STEPS + 1))
    with pytest.raises(ValidationError, match="at most 100 items"):
        ScenarioDefinition(
            scenario="metadata",
            metadata={str(index): index for index in range(MAX_SCENARIO_METADATA_ENTRIES + 1)},
            steps=[step],
        )


def test_tag_filter_supports_all_any_exclusion_and_case_insensitive_matching() -> None:
    scenarios = [
        ScenarioDefinition.model_construct(
            scenario="team_a_smoke",
            tags=["Database", "Smoke"],
            steps=[],
        ),
        ScenarioDefinition.model_construct(
            scenario="team_b_regression",
            tags=["database", "regression"],
            steps=[],
        ),
        ScenarioDefinition.model_construct(
            scenario="api_smoke",
            tags=["api", "smoke"],
            steps=[],
        ),
    ]
    tag_filter = ScenarioTagFilter.create(
        required_all=["DATABASE"],
        required_any=["smoke", "regression"],
        excluded=["regression"],
    )

    selected = select_scenarios(scenarios, tag_filter)

    assert [scenario.scenario for scenario in selected] == ["team_a_smoke"]


def test_tag_filter_rejects_conflicts_and_empty_results() -> None:
    with pytest.raises(ValueError, match="required tag cannot also be excluded"):
        ScenarioTagFilter.create(required_all=["smoke"], excluded=["SMOKE"])

    tag_filter = ScenarioTagFilter.create(required_all=["missing"])
    scenario = ScenarioDefinition.model_construct(
        scenario="smoke",
        tags=["smoke"],
        steps=[],
    )
    with pytest.raises(ScenarioLoadError, match="No scenarios matched"):
        select_scenarios([scenario], tag_filter)


def test_cli_validate_filters_a_recursive_directory_by_tags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    team = settings.scenarios_dir / "team"
    team.mkdir()
    (team / "database.yaml").write_text(
        """scenario: "Database smoke"
tags: [database, smoke]
steps:
  - capturePageSnapshot:
      id: page
      url: https://example.test
""",
        encoding="utf-8",
    )
    (team / "api.yaml").write_text(
        """scenario: "API smoke"
tags: [api, smoke]
steps:
  - capturePageSnapshot:
      id: page
      url: https://example.test
""",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        scenarios=[Path("team")],
        required_tags=["database", "smoke"],
        any_tags=[],
        excluded_tags=[],
    )

    status = cli._validate(args, settings)

    output = capsys.readouterr().out
    assert status == 0
    assert "database.yaml" in output
    assert "api.yaml" not in output


def test_cli_validate_rejects_invalid_activity_parameters_before_execution(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    invalid = settings.scenarios_dir / "invalid_database.yaml"
    invalid.write_text(
        """scenario: "Invalid database phase"
steps:
  - discoverDatabase:
      id: invalid_phase
      source:
        username: "env:APP_DB_USERNAME"
        password: "env:APP_DB_PASSWORD"
        dbUrl: "env:APP_DB_URL"
      phase: data
""",
        encoding="utf-8",
    )
    args = argparse.Namespace(scenarios=[invalid])

    with pytest.raises(PlantainError, match="parameters are invalid"):
        cli._validate(args, settings)


def test_scenario_result_exposes_tags_in_sanitized_reports() -> None:
    result = ScenarioResult(
        scenario="Database smoke",
        status="passed",
        duration_ms=1,
        tags=["database", "smoke"],
    )

    assert result.sanitized_dict()["tags"] == ["database", "smoke"]


def test_run_many_uses_bounded_workers_and_preserves_result_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    scenarios = [
        ScenarioDefinition.model_construct(scenario=f"scenario_{index}", steps=[])
        for index in range(SCENARIO_COUNT)
    ]
    active = 0
    peak = 0

    async def fake_run(_self: ScenarioRunner, scenario: ScenarioDefinition) -> ScenarioResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            if scenario.scenario == "scenario_1":
                raise RuntimeError("expected isolated failure")
            return ScenarioResult(
                scenario=scenario.scenario,
                status="passed",
                duration_ms=1,
            )
        finally:
            active -= 1

    monkeypatch.setattr(ScenarioRunner, "run", fake_run)
    runner = ScenarioRunner(settings, ActivityRegistry())

    results = asyncio.run(runner.run_many(scenarios, concurrency=EXPECTED_CONCURRENCY))

    assert peak == EXPECTED_CONCURRENCY
    assert cast("ScenarioResult", results[0]).scenario == "scenario_0"
    assert isinstance(results[1], RuntimeError)
    assert cast("ScenarioResult", results[2]).scenario == "scenario_2"
    assert cast("ScenarioResult", results[3]).scenario == "scenario_3"


def test_run_many_lazily_shares_and_closes_one_database_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    scenarios = [
        ScenarioDefinition.model_construct(scenario=f"scenario_{index}", steps=[])
        for index in range(SCENARIO_COUNT)
    ]
    created: list[object] = []
    observed: list[object] = []

    class FakePool:
        def __init__(self, _settings: Settings) -> None:
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True

    async def immediate_to_thread(function: object, *args: object) -> object:
        return cast("Any", function)(*args)

    async def fake_run(self: ScenarioRunner, scenario: ScenarioDefinition) -> ScenarioResult:
        observed.append(self._active_database_pools())
        await asyncio.sleep(0)
        return ScenarioResult(
            scenario=scenario.scenario,
            status="passed",
            duration_ms=1,
        )

    monkeypatch.setattr("plantain.engine.runner.DatabasePoolManager", FakePool)
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(ScenarioRunner, "run", fake_run)
    runner = ScenarioRunner(settings, ActivityRegistry())

    results = asyncio.run(runner.run_many(scenarios, concurrency=EXPECTED_CONCURRENCY))

    assert len(results) == SCENARIO_COUNT
    assert len(created) == 1
    assert observed == [created[0]] * SCENARIO_COUNT
    assert cast("Any", created[0]).closed is True
