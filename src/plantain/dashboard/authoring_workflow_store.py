"""Bounded backend-only retention for progressive scenario-authoring workflows."""

from __future__ import annotations

import copy
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Generic, TypeVar

from plantain.dashboard.agent.models import (
    AgentCapability,
    IntentDecision,
    UserIntent,
)

WORKFLOW_ID_BYTES = 16
WORKFLOW_ID_ATTEMPTS = 8
MAX_BOUND_SCENARIO_ID_BYTES = 64

_WorkflowError = TypeVar("_WorkflowError", bound=Exception)


@dataclass(frozen=True, slots=True, repr=False)
class AuthoringWorkflowRecord:
    """Private state needed to continue one progressive authoring workflow."""

    workflow_id: str
    intent: UserIntent
    decision: IntentDecision
    created_at: float
    expires_at: float
    retained_bytes: int
    scenario_id: str | None = None


class AuthoringWorkflowStore(Generic[_WorkflowError]):
    """Keep bounded authoring state behind opaque, expiring identifiers."""

    def __init__(
        self,
        *,
        capability: AgentCapability,
        name: str,
        decision_name: str,
        error_type: type[_WorkflowError],
        max_entries: int,
        max_total_bytes: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        if max_entries < 1 or max_total_bytes < 1 or ttl_seconds <= 0:
            raise ValueError("Authoring workflow limits must be positive")
        self._capability = capability
        self._name = name
        self._decision_name = decision_name
        self._error_type = error_type
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._token_factory = token_factory
        self._records: OrderedDict[str, AuthoringWorkflowRecord] = OrderedDict()
        self._retained_bytes = 0
        self._lock = threading.Lock()

    def put(self, intent: UserIntent, decision: IntentDecision) -> str:
        """Retain a defensive copy of one capability-specific workflow."""

        self._validate_decision(decision)
        retained_intent = intent.model_copy(deep=True)
        retained_decision = decision.model_copy(deep=True)
        retained_bytes = len(retained_intent.model_dump_json().encode()) + len(
            retained_decision.model_dump_json().encode()
        )
        if retained_bytes > self._max_total_bytes:
            raise self._error(f"The {self._name} workflow is too large to retain safely")
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            workflow_id = self._new_identifier()
            record = AuthoringWorkflowRecord(
                workflow_id=workflow_id,
                intent=retained_intent,
                decision=retained_decision,
                created_at=now,
                expires_at=now + self._ttl_seconds,
                retained_bytes=retained_bytes,
            )
            self._records[workflow_id] = record
            self._retained_bytes += retained_bytes
            self._enforce_limits()
        return workflow_id

    def bind(self, workflow_id: str, scenario_id: str) -> None:
        """Bind a workflow to the exact saved scenario it may continue."""

        self._validate_identifier(workflow_id)
        self._validate_scenario_id(scenario_id)
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            record = self._records.get(workflow_id)
            if record is None:
                raise self._unavailable()
            if record.scenario_id is not None and not hmac.compare_digest(
                record.scenario_id,
                scenario_id,
            ):
                raise self._error(f"The {self._name} workflow belongs to another scenario")
            self._records[workflow_id] = replace(record, scenario_id=scenario_id)
            self._records.move_to_end(workflow_id)

    def load(self, workflow_id: str, scenario_id: str) -> AuthoringWorkflowRecord:
        """Return a defensive copy after exact workflow/scenario proof."""

        self._validate_identifier(workflow_id)
        self._validate_scenario_id(scenario_id)
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            record = self._records.get(workflow_id)
            if record is None:
                raise self._unavailable()
            if record.scenario_id is None or not hmac.compare_digest(
                record.scenario_id,
                scenario_id,
            ):
                raise self._error(f"The {self._name} workflow does not belong to this scenario")
            self._records.move_to_end(workflow_id)
            return copy.deepcopy(record)

    def rebind(
        self,
        workflow_id: str,
        current_scenario_id: str,
        new_scenario_id: str,
    ) -> None:
        """Move ownership only after proving the workflow's current scenario."""

        self._validate_identifier(workflow_id)
        self._validate_scenario_id(current_scenario_id)
        self._validate_scenario_id(new_scenario_id)
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            record = self._records.get(workflow_id)
            if (
                record is None
                or record.scenario_id is None
                or not hmac.compare_digest(record.scenario_id, current_scenario_id)
            ):
                raise self._error(f"The {self._name} workflow does not belong to this scenario")
            self._records[workflow_id] = replace(
                record,
                scenario_id=new_scenario_id,
            )
            self._records.move_to_end(workflow_id)

    def discard(self, workflow_id: str) -> bool:
        """Forget a workflow without revealing whether it existed."""

        if not is_valid_authoring_workflow_id(workflow_id):
            return False
        with self._lock:
            record = self._records.pop(workflow_id, None)
            if record is not None:
                self._retained_bytes -= record.retained_bytes
                return True
        return False

    def _validate_decision(self, decision: IntentDecision) -> None:
        if decision.capability is not self._capability:
            raise self._error(
                f"The {self._name} workflow requires a {self._decision_name} decision"
            )

    def _validate_identifier(self, workflow_id: str) -> None:
        if not is_valid_authoring_workflow_id(workflow_id):
            raise self._error(f"The {self._name} workflow identifier is invalid")

    def _validate_scenario_id(self, scenario_id: str) -> None:
        if (
            not isinstance(scenario_id, str)
            or len(scenario_id) != MAX_BOUND_SCENARIO_ID_BYTES
            or any(character not in "0123456789abcdef" for character in scenario_id)
        ):
            raise self._error(f"The {self._name} scenario identity is invalid")

    def _new_identifier(self) -> str:
        for _ in range(WORKFLOW_ID_ATTEMPTS):
            candidate = self._token_factory(WORKFLOW_ID_BYTES)
            if is_valid_authoring_workflow_id(candidate) and candidate not in self._records:
                return candidate
        raise self._error(f"Unable to allocate the {self._name} workflow safely")

    def _discard_expired(self, now: float) -> None:
        expired = [
            workflow_id for workflow_id, record in self._records.items() if record.expires_at <= now
        ]
        for workflow_id in expired:
            record = self._records.pop(workflow_id)
            self._retained_bytes -= record.retained_bytes

    def _enforce_limits(self) -> None:
        while (
            len(self._records) > self._max_entries or self._retained_bytes > self._max_total_bytes
        ):
            _workflow_id, record = self._records.popitem(last=False)
            self._retained_bytes -= record.retained_bytes

    def _unavailable(self) -> _WorkflowError:
        return self._error(f"The {self._name} workflow is unavailable or expired; start it again")

    def _error(self, message: str) -> _WorkflowError:
        return self._error_type(message)


def is_valid_authoring_workflow_id(value: str) -> bool:
    """Return whether a value is one opaque workflow identifier."""

    if not isinstance(value, str) or len(value) != WORKFLOW_ID_BYTES * 2:
        return False
    return all(character in "0123456789abcdef" for character in value)


__all__ = [
    "MAX_BOUND_SCENARIO_ID_BYTES",
    "WORKFLOW_ID_ATTEMPTS",
    "WORKFLOW_ID_BYTES",
    "AuthoringWorkflowRecord",
    "AuthoringWorkflowStore",
    "is_valid_authoring_workflow_id",
]
