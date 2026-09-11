"""Backend-only database workflows remain bounded, expiring, and scenario-bound."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from plantain.dashboard.agent.models import (
    AgentCapability,
    IntentAction,
    IntentDecision,
    UserIntent,
)
from plantain.dashboard.database_workflow_store import (
    DashboardDatabaseWorkflowError,
    DatabaseWorkflowStore,
)

FIRST_WORKFLOW_ID = "a" * 32
SECOND_WORKFLOW_ID = "b" * 32
FIRST_SCENARIO_ID = "c" * 64
SECOND_SCENARIO_ID = "d" * 64


def _decision(
    capability: AgentCapability = AgentCapability.DATABASE_DISCOVERY,
) -> IntentDecision:
    return IntentDecision(
        action=IntentAction.PLAN,
        capability=capability,
        summary="Discover database metadata for a customer-state test.",
        plan_steps=["Discover schemas before selecting an object."],
    )


def _tokens(*values: str) -> Callable[[int], str]:
    iterator: Iterator[str] = iter(values)
    return lambda _byte_count: next(iterator)


def test_workflow_requires_exact_bound_scenario_and_returns_defensive_copies() -> None:
    store = DatabaseWorkflowStore(token_factory=_tokens(FIRST_WORKFLOW_ID))
    intent = UserIntent(prompt="Find the customer state needed for this test.")

    workflow_id = store.put(intent, _decision())
    store.bind(workflow_id, FIRST_SCENARIO_ID)
    record = store.load(workflow_id, FIRST_SCENARIO_ID)
    record.intent.prompt = "changed outside the store"

    retained = store.load(workflow_id, FIRST_SCENARIO_ID)
    assert retained.intent.prompt == intent.prompt
    assert retained.scenario_id == FIRST_SCENARIO_ID
    assert intent.prompt not in repr(retained)

    with pytest.raises(DashboardDatabaseWorkflowError, match="does not belong"):
        store.load(workflow_id, SECOND_SCENARIO_ID)

    store.rebind(workflow_id, FIRST_SCENARIO_ID, SECOND_SCENARIO_ID)
    assert store.load(workflow_id, SECOND_SCENARIO_ID).scenario_id == SECOND_SCENARIO_ID
    with pytest.raises(DashboardDatabaseWorkflowError, match="does not belong"):
        store.rebind(workflow_id, FIRST_SCENARIO_ID, SECOND_SCENARIO_ID)


def test_unbound_expired_and_invalid_workflows_fail_closed() -> None:
    now = [10.0]
    store = DatabaseWorkflowStore(
        ttl_seconds=5.0,
        clock=lambda: now[0],
        token_factory=_tokens(FIRST_WORKFLOW_ID),
    )
    workflow_id = store.put(UserIntent(prompt="Discover schemas."), _decision())

    with pytest.raises(DashboardDatabaseWorkflowError, match="does not belong"):
        store.load(workflow_id, FIRST_SCENARIO_ID)

    now[0] = 16.0
    with pytest.raises(DashboardDatabaseWorkflowError, match="expired"):
        store.bind(workflow_id, FIRST_SCENARIO_ID)

    with pytest.raises(DashboardDatabaseWorkflowError, match="identifier is invalid"):
        store.load("invalid", FIRST_SCENARIO_ID)


def test_store_evicts_oldest_workflow_within_fixed_entry_bound() -> None:
    store = DatabaseWorkflowStore(
        max_entries=1,
        token_factory=_tokens(FIRST_WORKFLOW_ID, SECOND_WORKFLOW_ID),
    )
    first = store.put(UserIntent(prompt="First database goal."), _decision())
    second = store.put(UserIntent(prompt="Second database goal."), _decision())
    store.bind(second, SECOND_SCENARIO_ID)

    with pytest.raises(DashboardDatabaseWorkflowError, match="expired"):
        store.bind(first, FIRST_SCENARIO_ID)
    assert store.load(second, SECOND_SCENARIO_ID).workflow_id == SECOND_WORKFLOW_ID


def test_store_rejects_non_database_decisions_and_oversized_records() -> None:
    intent = UserIntent(prompt="Discover schemas.")
    store = DatabaseWorkflowStore(token_factory=_tokens(FIRST_WORKFLOW_ID))

    with pytest.raises(DashboardDatabaseWorkflowError, match="database authoring decision"):
        store.put(intent, _decision(AgentCapability.UI_DISCOVERY))

    constrained = DatabaseWorkflowStore(
        max_total_bytes=1,
        token_factory=_tokens(FIRST_WORKFLOW_ID),
    )
    with pytest.raises(DashboardDatabaseWorkflowError, match="too large"):
        constrained.put(intent, _decision())


def test_discard_is_idempotent_and_rejects_malformed_identifiers() -> None:
    store = DatabaseWorkflowStore(token_factory=_tokens(FIRST_WORKFLOW_ID))
    workflow_id = store.put(UserIntent(prompt="Discover schemas."), _decision())

    assert store.discard(workflow_id) is True
    assert store.discard(workflow_id) is False
    assert store.discard("invalid") is False
