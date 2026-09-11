"""User-friendly generated-scenario path normalization."""

from __future__ import annotations

import pytest

from plantain.dashboard.agent.models import AgentCapability
from plantain.dashboard.scenario_paths import (
    DashboardScenarioPathError,
    default_scenario_directory,
    normalize_scenario_directory,
    scenario_filename_stem,
)


def test_missing_folder_uses_capability_default() -> None:
    assert normalize_scenario_directory(None, AgentCapability.API_CONTRACT) == "generated/api"
    assert default_scenario_directory(AgentCapability.UI_DISCOVERY) == "generated/ui"


def test_friendly_folder_and_filename_are_normalized() -> None:
    assert (
        normalize_scenario_directory(
            r"Payments Team\Regression Cases",
            AgentCapability.AUTOMATION_GENERATION,
        )
        == "payments-team/regression-cases"
    )
    assert scenario_filename_stem("Checkout — Guest & Member") == "checkout-guest-member"
    assert scenario_filename_stem("🧪") == "scenario"


@pytest.mark.parametrize(
    "value",
    [
        "/outside",
        r"C:\outside",
        r"\\server\share",
        "~/outside",
        "../outside",
        "safe/../../outside",
        "...",
    ],
)
def test_unsafe_or_empty_folder_is_rejected(value: str) -> None:
    with pytest.raises(DashboardScenarioPathError):
        normalize_scenario_directory(value, AgentCapability.API_CONTRACT)


def test_non_scenario_capability_has_no_default_folder() -> None:
    with pytest.raises(
        DashboardScenarioPathError,
        match="cannot save scenario YAML",
    ):
        default_scenario_directory(AgentCapability.WORKSPACE_QUESTION)
