"""Scenario detail remains current, bounded, redacted, and browser-safe."""

from __future__ import annotations

from secrets import token_hex
from types import SimpleNamespace
from typing import Any

import pytest

from plantain.dashboard import scenario_detail
from plantain.dashboard.scenario_detail import (
    ScenarioDetailError,
    load_scenario_detail,
)
from plantain.errors import ScenarioLoadError
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.security.redaction import REDACTED

SCENARIO_ID = "a" * 64
OBSERVED_VALUE = token_hex(16)
EXPECTED_FIRST_PAGE_STEPS = 2
EXPECTED_PAGE_COUNT = 2


def _scenario() -> ScenarioDefinition:
    return ScenarioDefinition.model_validate(
        {
            "scenario": "Checkout",
            "tags": ["smoke"],
            "steps": [
                StepDefinition.from_yaml(
                    {
                        "sendRequest": {
                            "id": "listPets",
                            "headers": {"Authorization": OBSERVED_VALUE},
                        }
                    }
                ),
                StepDefinition.from_yaml({"validateSchema": {"id": "validatePets"}}),
                StepDefinition.from_yaml({"capturePageSnapshot": {"id": "showPets"}}),
            ],
        }
    )


def _install(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    definition: ScenarioDefinition,
) -> None:
    scenarios_dir = tmp_path / "scenarios"
    target = scenarios_dir / "api/checkout.yaml"
    settings = SimpleNamespace(
        scenarios_dir=scenarios_dir,
        sensitive_key_names=(),
    )
    monkeypatch.setattr(
        scenario_detail,
        "Settings",
        SimpleNamespace(from_env=lambda _root: settings),
    )
    monkeypatch.setattr(
        scenario_detail,
        "dashboard_reporting_runtime",
        lambda configured: SimpleNamespace(settings=configured),
    )
    monkeypatch.setattr(
        scenario_detail,
        "resolve_scenario_path",
        lambda _root, _scenario_id: target,
    )
    monkeypatch.setattr(
        scenario_detail,
        "load_scenario",
        lambda _path, _settings: definition,
    )


def test_detail_pages_reconstructed_redacted_steps(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(tmp_path, monkeypatch, _scenario())

    first = load_scenario_detail(tmp_path, SCENARIO_ID, page_size=2)
    second = load_scenario_detail(tmp_path, SCENARIO_ID, page=2, page_size=2)

    assert first.name == "Checkout"
    assert first.source == "api/checkout.yaml"
    assert first.page_count == EXPECTED_PAGE_COUNT
    assert first.has_next is True
    assert len(first.steps) == EXPECTED_FIRST_PAGE_STEPS
    assert first.steps[0].position == 1
    assert "- sendRequest:" in first.steps[0].yaml_text
    assert OBSERVED_VALUE not in first.steps[0].yaml_text
    assert REDACTED in first.steps[0].yaml_text
    assert [step.position for step in second.steps] == [3]
    assert second.has_previous is True


def test_detail_marks_individually_limited_step_previews(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = ScenarioDefinition.model_validate(
        {
            "scenario": "Large step",
            "steps": [StepDefinition.from_yaml({"sendRequest": {"body": "x" * 512}})],
        }
    )
    _install(tmp_path, monkeypatch, definition)
    monkeypatch.setattr(scenario_detail, "MAX_SCENARIO_STEP_DETAIL_BYTES", 96)

    page = load_scenario_detail(tmp_path, SCENARIO_ID)

    assert page.steps[0].content_limited is True
    assert "Preview limited" in page.steps[0].yaml_text
    assert "explicit byte limit" in page.notice


def test_detail_rejects_invalid_browser_requests(tmp_path: Any) -> None:
    with pytest.raises(ScenarioDetailError, match="identifier is invalid"):
        load_scenario_detail(tmp_path, "../scenario.yaml")
    with pytest.raises(ScenarioDetailError, match="positive integer"):
        load_scenario_detail(tmp_path, SCENARIO_ID, page=0)
    with pytest.raises(ScenarioDetailError, match="page size"):
        load_scenario_detail(tmp_path, SCENARIO_ID, page_size=25)


def test_detail_translates_loader_failures_without_echo(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(tmp_path, monkeypatch, _scenario())

    def reject_load(*_args: Any) -> ScenarioDefinition:
        raise ScenarioLoadError(OBSERVED_VALUE)

    monkeypatch.setattr(scenario_detail, "load_scenario", reject_load)

    with pytest.raises(ScenarioDetailError, match="loaded safely") as captured:
        load_scenario_detail(tmp_path, SCENARIO_ID)
    assert OBSERVED_VALUE not in str(captured.value)
