"""Bounded process-owned state for guided API operation selection."""

from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from plantain.dashboard.agent.api_models import (
    ApiContractInspection,
    ApiWorkflowKind,
)
from plantain.dashboard.agent.models import IntentDecision, UserIntent
from plantain.errors import PlantainError

API_INSPECTION_ID_BYTES = 16
API_INSPECTION_ID_GENERATION_ATTEMPTS = 8
API_INSPECTION_TTL_SECONDS = 3_600.0
MAX_API_INSPECTION_STORE_BYTES = 8_388_608
MAX_STORED_API_INSPECTIONS = 32
_INSPECTION_ID = re.compile(r"^[a-f0-9]{32}$")
_OPERATION_KEY = re.compile(r"^[a-f0-9]{64}$")


class DashboardApiInspectionError(PlantainError):
    """Raised when a guided API selection is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class ApiInspectionRecord:
    """Server-only context for an operation previously offered to the user."""

    inspection_id: str
    schema_id: str
    intent: UserIntent = field(repr=False)
    decision: IntentDecision = field(repr=False)


@dataclass(frozen=True, slots=True)
class _StoredApiInspection:
    schema_id: str
    operation_keys: tuple[str, ...]
    intent: UserIntent = field(repr=False)
    decision: IntentDecision = field(repr=False)
    byte_count: int
    expires_at: float


class ApiInspectionStore:
    """Retain guided API selections within fixed memory and lifetime bounds."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_STORED_API_INSPECTIONS,
        max_total_bytes: int = MAX_API_INSPECTION_STORE_BYTES,
        ttl_seconds: float = API_INSPECTION_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        if max_entries <= 0 or max_total_bytes <= 0 or ttl_seconds <= 0:
            raise ValueError("API inspection store bounds must be positive")
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._token_factory = token_factory
        self._lock = Lock()
        self._entries: OrderedDict[str, _StoredApiInspection] = OrderedDict()
        self._total_bytes = 0

    def put(
        self,
        inspection: ApiContractInspection,
        intent: UserIntent,
        decision: IntentDecision,
    ) -> str:
        """Store one ambiguous inspection and return an opaque identifier."""

        if not inspection.operations:
            raise DashboardApiInspectionError("The API inspection has no selectable operations")
        if (
            decision.api_workflow is not ApiWorkflowKind.CONTRACT
            or decision.api_schema_reference is None
        ):
            raise DashboardApiInspectionError("The API inspection context is incomplete")
        operation_keys = tuple(item.operation_key for item in inspection.operations)
        if len(operation_keys) != len(set(operation_keys)):
            raise DashboardApiInspectionError("The API inspection contains duplicate operations")
        retained_intent = intent.model_copy(deep=True)
        retained_decision = decision.model_copy(deep=True)
        byte_count = _retained_bytes(
            inspection.schema_id,
            operation_keys,
            retained_intent,
            retained_decision,
        )
        if byte_count > self._max_total_bytes:
            raise DashboardApiInspectionError("The API inspection is too large to retain safely")
        with self._lock:
            now = self._clock()
            self._remove_expired(now)
            self._make_room(byte_count)
            inspection_id = self._new_id()
            self._entries[inspection_id] = _StoredApiInspection(
                schema_id=inspection.schema_id,
                operation_keys=operation_keys,
                intent=retained_intent,
                decision=retained_decision,
                byte_count=byte_count,
                expires_at=now + self._ttl_seconds,
            )
            self._total_bytes += byte_count
        return inspection_id

    def load(self, inspection_id: str, operation_key: str) -> ApiInspectionRecord:
        """Resolve one offered operation and refresh its bounded lifetime."""

        if _INSPECTION_ID.fullmatch(inspection_id) is None:
            raise DashboardApiInspectionError("The API inspection identifier is invalid")
        if _OPERATION_KEY.fullmatch(operation_key) is None:
            raise DashboardApiInspectionError("The selected API operation is invalid")
        with self._lock:
            now = self._clock()
            self._remove_expired(now)
            stored = self._entries.get(inspection_id)
            if stored is None:
                raise DashboardApiInspectionError(
                    "This API inspection expired; inspect the contract again"
                )
            if operation_key not in stored.operation_keys:
                raise DashboardApiInspectionError("The selected API operation was not offered")
            refreshed = _StoredApiInspection(
                schema_id=stored.schema_id,
                operation_keys=stored.operation_keys,
                intent=stored.intent,
                decision=stored.decision,
                byte_count=stored.byte_count,
                expires_at=now + self._ttl_seconds,
            )
            self._entries[inspection_id] = refreshed
            self._entries.move_to_end(inspection_id)
            return ApiInspectionRecord(
                inspection_id=inspection_id,
                schema_id=refreshed.schema_id,
                intent=refreshed.intent.model_copy(deep=True),
                decision=refreshed.decision.model_copy(deep=True),
            )

    def discard(self, inspection_id: str) -> bool:
        """Forget one inspection after authoring or explicit replacement."""

        if _INSPECTION_ID.fullmatch(inspection_id) is None:
            return False
        with self._lock:
            return self._remove(inspection_id)

    def _remove_expired(self, now: float) -> None:
        while self._entries:
            inspection_id, stored = next(iter(self._entries.items()))
            if stored.expires_at > now:
                return
            self._remove(inspection_id)

    def _make_room(self, byte_count: int) -> None:
        while self._entries and (
            len(self._entries) >= self._max_entries
            or self._total_bytes + byte_count > self._max_total_bytes
        ):
            self._remove(next(iter(self._entries)))

    def _remove(self, inspection_id: str) -> bool:
        stored = self._entries.pop(inspection_id, None)
        if stored is None:
            return False
        self._total_bytes -= stored.byte_count
        return True

    def _new_id(self) -> str:
        for _attempt in range(API_INSPECTION_ID_GENERATION_ATTEMPTS):
            candidate = self._token_factory(API_INSPECTION_ID_BYTES)
            if _INSPECTION_ID.fullmatch(candidate) is not None and candidate not in self._entries:
                return candidate
        raise DashboardApiInspectionError("Plantain could not allocate an API inspection safely")


def _retained_bytes(
    schema_id: str,
    operation_keys: tuple[str, ...],
    intent: UserIntent,
    decision: IntentDecision,
) -> int:
    return sum(
        len(value.encode())
        for value in (
            schema_id,
            *operation_keys,
            intent.model_dump_json(),
            decision.model_dump_json(),
        )
    )


_api_inspection_store = ApiInspectionStore()


def store_api_inspection(
    inspection: ApiContractInspection,
    intent: UserIntent,
    decision: IntentDecision,
) -> str:
    """Retain an ambiguous contract inspection for a later explicit choice."""

    return _api_inspection_store.put(inspection, intent, decision)


def load_api_inspection(
    inspection_id: str,
    operation_key: str,
) -> ApiInspectionRecord:
    """Resolve a browser selection against the operations previously offered."""

    return _api_inspection_store.load(inspection_id, operation_key)


def discard_api_inspection(inspection_id: str) -> bool:
    """Discard one dashboard-owned API inspection."""

    return _api_inspection_store.discard(inspection_id)


__all__ = [
    "ApiInspectionRecord",
    "ApiInspectionStore",
    "DashboardApiInspectionError",
    "discard_api_inspection",
    "load_api_inspection",
    "store_api_inspection",
]
