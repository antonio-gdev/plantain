"""Saved progressive workflows remain bound to exact compatible scenarios."""

from __future__ import annotations

from typing import Any

import pytest

from plantain.dashboard import state
from plantain.dashboard.agent.models import AgentCapability
from plantain.dashboard.scenario_persistence import SavedScenario
from plantain.dashboard.ui_workflow_store import DashboardUiWorkflowError

WORKFLOW_ID = "a" * 32
FIRST_SCENARIO_ID = "b" * 64
SECOND_SCENARIO_ID = "c" * 64


def _saved(
    scenario_id: str,
    *,
    activities: tuple[str, ...] = ("capturePageSnapshot",),
) -> SavedScenario:
    return SavedScenario(
        scenario_id=scenario_id,
        scenario="Discover checkout",
        relative_path="generated/ui/discover-checkout.yaml",
        capability=AgentCapability.UI_DISCOVERY,
        step_count=1,
        activities=activities,
    )


def test_ui_workflow_is_initially_bound_then_exactly_rebound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        state,
        "bind_ui_workflow",
        lambda *values: calls.append(("bind", *values)),
    )
    monkeypatch.setattr(
        state,
        "rebind_ui_workflow",
        lambda *values: calls.append(("rebind", *values)),
    )

    initial = state._bind_saved_workflows(
        "",
        "",
        WORKFLOW_ID,
        "",
        _saved(FIRST_SCENARIO_ID),
    )
    continued = state._bind_saved_workflows(
        "",
        "",
        WORKFLOW_ID,
        FIRST_SCENARIO_ID,
        _saved(SECOND_SCENARIO_ID),
    )

    assert initial == ("", "", WORKFLOW_ID, FIRST_SCENARIO_ID, "")
    assert continued == ("", "", WORKFLOW_ID, SECOND_SCENARIO_ID, "")
    assert calls == [
        ("bind", WORKFLOW_ID, FIRST_SCENARIO_ID),
        ("rebind", WORKFLOW_ID, FIRST_SCENARIO_ID, SECOND_SCENARIO_ID),
    ]


def test_ui_binding_failure_discards_only_that_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discarded: list[str] = []

    def reject(*_values: str) -> None:
        raise DashboardUiWorkflowError("synthetic private detail")

    monkeypatch.setattr(state, "bind_ui_workflow", reject)
    monkeypatch.setattr(state, "discard_ui_workflow", discarded.append)

    bound = state._bind_saved_workflows(
        "",
        "",
        WORKFLOW_ID,
        "",
        _saved(FIRST_SCENARIO_ID),
    )

    assert bound == (
        "",
        "",
        "",
        "",
        " Automatic UI continuation is unavailable.",
    )
    assert discarded == [WORKFLOW_ID]


def test_ui_workflow_is_discarded_for_an_incompatible_saved_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discarded: list[str] = []
    monkeypatch.setattr(state, "discard_ui_workflow", discarded.append)

    bound = state._bind_saved_workflows(
        "",
        "",
        WORKFLOW_ID,
        "",
        _saved(FIRST_SCENARIO_ID, activities=("sendRequest",)),
    )

    assert bound == ("", "", "", "", "")
    assert discarded == [WORKFLOW_ID]
