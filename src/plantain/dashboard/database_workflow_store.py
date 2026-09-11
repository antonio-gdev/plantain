"""Bounded backend-only state for progressive database authoring."""

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

DATABASE_WORKFLOW_MAX_ENTRIES = 32
DATABASE_WORKFLOW_MAX_TOTAL_BYTES = 2 * 1024 * 1024
DATABASE_WORKFLOW_TTL_SECONDS = 60 * 60


class DashboardDatabaseWorkflowError(Exception):
    """Raised when a retained database workflow cannot be used safely."""


class DatabaseWorkflowStore(AuthoringWorkflowStore[DashboardDatabaseWorkflowError]):
    """Keep database workflow state in one isolated bounded store."""

    def __init__(
        self,
        *,
        max_entries: int = DATABASE_WORKFLOW_MAX_ENTRIES,
        max_total_bytes: int = DATABASE_WORKFLOW_MAX_TOTAL_BYTES,
        ttl_seconds: float = DATABASE_WORKFLOW_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        super().__init__(
            capability=AgentCapability.DATABASE_DISCOVERY,
            name="database discovery",
            decision_name="database authoring",
            error_type=DashboardDatabaseWorkflowError,
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
            ttl_seconds=ttl_seconds,
            clock=clock,
            token_factory=token_factory,
        )


DatabaseWorkflowRecord = AuthoringWorkflowRecord
_STORE = DatabaseWorkflowStore()


def store_database_workflow(intent: UserIntent, decision: IntentDecision) -> str:
    """Retain one database workflow and return only its opaque identifier."""

    return _STORE.put(intent, decision)


def bind_database_workflow(workflow_id: str, scenario_id: str) -> None:
    """Bind a workflow to the exact saved scenario it may continue."""

    _STORE.bind(workflow_id, scenario_id)


def load_database_workflow(
    workflow_id: str,
    scenario_id: str,
) -> DatabaseWorkflowRecord:
    """Load one exactly bound database workflow."""

    return _STORE.load(workflow_id, scenario_id)


def rebind_database_workflow(
    workflow_id: str,
    current_scenario_id: str,
    new_scenario_id: str,
) -> None:
    """Move a workflow after proving its currently bound scenario."""

    _STORE.rebind(workflow_id, current_scenario_id, new_scenario_id)


def discard_database_workflow(workflow_id: str) -> bool:
    """Discard one database workflow idempotently."""

    return _STORE.discard(workflow_id)


def is_valid_database_workflow_id(value: str) -> bool:
    """Return whether a value is one opaque database workflow identifier."""

    return is_valid_authoring_workflow_id(value)


__all__ = [
    "DATABASE_WORKFLOW_TTL_SECONDS",
    "DashboardDatabaseWorkflowError",
    "DatabaseWorkflowRecord",
    "DatabaseWorkflowStore",
    "bind_database_workflow",
    "discard_database_workflow",
    "is_valid_database_workflow_id",
    "load_database_workflow",
    "rebind_database_workflow",
    "store_database_workflow",
]
