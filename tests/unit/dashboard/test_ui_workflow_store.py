"""Backend-only UI workflows remain bounded, expiring, and scenario-bound."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from plantain.dashboard.agent.models import (
    AgentCapability,
    IntentAction,
    IntentDecision,
    UserIntent,
)
from plantain.dashboard.ui_workflow_store import (
    DashboardUiWorkflowError,
    UiWorkflowStore,
)

FIRST_WORKFLOW_ID = "a" * 32
SECOND_WORKFLOW_ID = "b" * 32
FIRST_SCENARIO_ID = "c" * 64
SECOND_SCENARIO_ID = "d" * 64


def _decision(
    capability: AgentCapability = AgentCapability.UI_DISCOVERY,
) -> IntentDecision:
    return IntentDecision(
        action=IntentAction.PLAN,
        capability=capability,
        summary="Discover the current UI before extending its test.",
        plan_steps=["Capture the page before selecting an interaction."],
    )


def _tokens(*values: str) -> Callable[[int], str]:
    iterator: Iterator[str] = iter(values)
    return lambda _byte_count: next(iterator)


def test_ui_workflow_requires_exact_scenario_and_returns_defensive_copies() -> None:
    store = UiWorkflowStore(token_factory=_tokens(FIRST_WORKFLOW_ID))
    intent = UserIntent(prompt="Discover and test the checkout journey.")

    workflow_id = store.put(intent, _decision())
    store.bind(workflow_id, FIRST_SCENARIO_ID)
    record = store.load(workflow_id, FIRST_SCENARIO_ID)
    record.intent.prompt = "changed outside the store"

    retained = store.load(workflow_id, FIRST_SCENARIO_ID)
    assert retained.intent.prompt == intent.prompt
    assert retained.workflow_id == FIRST_WORKFLOW_ID
    assert retained.scenario_id == FIRST_SCENARIO_ID
    assert intent.prompt not in repr(retained)

    with pytest.raises(DashboardUiWorkflowError, match="does not belong"):
        store.load(workflow_id, SECOND_SCENARIO_ID)

    store.rebind(workflow_id, FIRST_SCENARIO_ID, SECOND_SCENARIO_ID)
    assert store.load(workflow_id, SECOND_SCENARIO_ID).scenario_id == SECOND_SCENARIO_ID
    with pytest.raises(DashboardUiWorkflowError, match="does not belong"):
        store.rebind(workflow_id, FIRST_SCENARIO_ID, SECOND_SCENARIO_ID)


def test_ui_workflow_rejects_unbound_expired_and_invalid_access() -> None:
    now = [10.0]
    store = UiWorkflowStore(
        ttl_seconds=5.0,
        clock=lambda: now[0],
        token_factory=_tokens(FIRST_WORKFLOW_ID),
    )
    workflow_id = store.put(UserIntent(prompt="Discover the login page."), _decision())

    with pytest.raises(DashboardUiWorkflowError, match="does not belong"):
        store.load(workflow_id, FIRST_SCENARIO_ID)

    now[0] = 16.0
    with pytest.raises(DashboardUiWorkflowError, match="expired"):
        store.bind(workflow_id, FIRST_SCENARIO_ID)

    with pytest.raises(DashboardUiWorkflowError, match="identifier is invalid"):
        store.load("invalid", FIRST_SCENARIO_ID)


def test_ui_store_evicts_the_oldest_workflow_within_its_entry_bound() -> None:
    store = UiWorkflowStore(
        max_entries=1,
        token_factory=_tokens(FIRST_WORKFLOW_ID, SECOND_WORKFLOW_ID),
    )
    first = store.put(UserIntent(prompt="Discover the first page."), _decision())
    second = store.put(UserIntent(prompt="Discover the second page."), _decision())
    store.bind(second, SECOND_SCENARIO_ID)

    with pytest.raises(DashboardUiWorkflowError, match="expired"):
        store.bind(first, FIRST_SCENARIO_ID)
    assert store.load(second, SECOND_SCENARIO_ID).workflow_id == SECOND_WORKFLOW_ID


def test_ui_store_rejects_non_ui_decisions_and_oversized_records() -> None:
    intent = UserIntent(prompt="Discover a page.")
    store = UiWorkflowStore(token_factory=_tokens(FIRST_WORKFLOW_ID))

    with pytest.raises(DashboardUiWorkflowError, match="UI authoring decision"):
        store.put(intent, _decision(AgentCapability.DATABASE_DISCOVERY))

    constrained = UiWorkflowStore(
        max_total_bytes=1,
        token_factory=_tokens(FIRST_WORKFLOW_ID),
    )
    with pytest.raises(DashboardUiWorkflowError, match="too large"):
        constrained.put(intent, _decision())


def test_ui_workflow_discard_is_idempotent() -> None:
    store = UiWorkflowStore(token_factory=_tokens(FIRST_WORKFLOW_ID))
    workflow_id = store.put(UserIntent(prompt="Discover a page."), _decision())

    assert store.discard(workflow_id) is True
    assert store.discard(workflow_id) is False
    assert store.discard("invalid") is False
