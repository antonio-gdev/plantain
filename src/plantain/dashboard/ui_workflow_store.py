"""Bounded backend-only state for progressive UI discovery and repair."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

from plantain.dashboard.agent.models import (
    AgentCapability,
    IntentDecision,
    UserIntent,
)
from plantain.dashboard.authoring_workflow_store import (
    AuthoringWorkflowRecord,
    AuthoringWorkflowStore,
    is_valid_authoring_workflow_id,
)

UI_WORKFLOW_MAX_ENTRIES = 32
UI_WORKFLOW_MAX_TOTAL_BYTES = 2 * 1024 * 1024
UI_WORKFLOW_TTL_SECONDS = 60 * 60


class DashboardUiWorkflowError(Exception):
    """Raised when a retained UI workflow cannot be used safely."""


class UiWorkflowStore(AuthoringWorkflowStore[DashboardUiWorkflowError]):
    """Keep UI workflow state in one isolated bounded store."""

    def __init__(
        self,
        *,
        max_entries: int = UI_WORKFLOW_MAX_ENTRIES,
        max_total_bytes: int = UI_WORKFLOW_MAX_TOTAL_BYTES,
        ttl_seconds: float = UI_WORKFLOW_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        super().__init__(
            capability=AgentCapability.UI_DISCOVERY,
            name="UI discovery",
            decision_name="UI authoring",
            error_type=DashboardUiWorkflowError,
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
            ttl_seconds=ttl_seconds,
            clock=clock,
            token_factory=token_factory,
        )


UiWorkflowRecord = AuthoringWorkflowRecord
_STORE = UiWorkflowStore()


def store_ui_workflow(intent: UserIntent, decision: IntentDecision) -> str:
    """Retain one UI workflow and return only its opaque identifier."""

    return _STORE.put(intent, decision)


def bind_ui_workflow(workflow_id: str, scenario_id: str) -> None:
    """Bind a workflow to the exact saved scenario it may continue."""

    _STORE.bind(workflow_id, scenario_id)


def load_ui_workflow(workflow_id: str, scenario_id: str) -> UiWorkflowRecord:
    """Load one exactly bound UI workflow."""

    return _STORE.load(workflow_id, scenario_id)


def rebind_ui_workflow(
    workflow_id: str,
    current_scenario_id: str,
    new_scenario_id: str,
) -> None:
    """Move a workflow after proving its currently bound scenario."""

    _STORE.rebind(workflow_id, current_scenario_id, new_scenario_id)


def discard_ui_workflow(workflow_id: str) -> bool:
    """Discard one UI workflow idempotently."""

    return _STORE.discard(workflow_id)


def is_valid_ui_workflow_id(value: str) -> bool:
    """Return whether a value is one opaque UI workflow identifier."""

    return is_valid_authoring_workflow_id(value)


__all__ = [
    "UI_WORKFLOW_TTL_SECONDS",
    "DashboardUiWorkflowError",
    "UiWorkflowRecord",
    "UiWorkflowStore",
    "bind_ui_workflow",
    "discard_ui_workflow",
    "is_valid_ui_workflow_id",
    "load_ui_workflow",
    "rebind_ui_workflow",
    "store_ui_workflow",
]
