"""Bounded backend ownership for guided API operation selections."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from plantain.dashboard.agent.api_models import (
    ApiContractInspection,
    ApiOperationCandidate,
    ApiWorkflowKind,
)
from plantain.dashboard.agent.models import (
    AgentCapability,
    IntentAction,
    IntentDecision,
    UserIntent,
)
from plantain.dashboard.api_inspection_store import (
    ApiInspectionStore,
    DashboardApiInspectionError,
)
from plantain.models.api import HttpMethod

FIRST_INSPECTION_ID = "a" * 32
SECOND_INSPECTION_ID = "b" * 32
FIRST_OPERATION_KEY = "c" * 64
SECOND_OPERATION_KEY = "d" * 64
UNKNOWN_OPERATION_KEY = "e" * 64
SCHEMA_ID = "f" * 64
TEST_TTL_SECONDS = 10.0
EXPIRED_TIME = 11.0
USER_PROMPT = "Use env:PETSTORE_SCHEMA_URL and create tests for the pet operations."


def _tokens(*values: str) -> tuple[Iterator[str], Callable[[int], str]]:
    iterator = iter(values)

    def token_factory(_byte_count: int) -> str:
        return next(iterator)

    return iterator, token_factory


def _intent() -> UserIntent:
    return UserIntent(prompt=USER_PROMPT)


def _decision() -> IntentDecision:
    return IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.API_CONTRACT,
        summary="Inspect the supplied pet contract.",
        plan_steps=["Inspect matching operations.", "Create a validated scenario."],
        api_workflow=ApiWorkflowKind.CONTRACT,
        api_schema_reference="env:PETSTORE_SCHEMA_URL",
        api_operation_query="pet operations",
    )


def _inspection() -> ApiContractInspection:
    return ApiContractInspection(
        schema_id=SCHEMA_ID,
        schema_version="3.0.3",
        source_url="https://api.example.test/openapi.json",
        base_url="https://api.example.test",
        query="pet operations",
        operations=[
            ApiOperationCandidate(
                operation_key=FIRST_OPERATION_KEY,
                operation_id="listPets",
                method=HttpMethod.GET,
                path="/pets",
                summary="List pets",
            ),
            ApiOperationCandidate(
                operation_key=SECOND_OPERATION_KEY,
                operation_id="getPet",
                method=HttpMethod.GET,
                path="/pets/{petId}",
                summary="Get one pet",
            ),
        ],
        total_matches=2,
    )


def test_store_returns_defensive_context_only_for_an_offered_operation() -> None:
    _iterator, token_factory = _tokens(FIRST_INSPECTION_ID)
    store = ApiInspectionStore(token_factory=token_factory)

    inspection_id = store.put(_inspection(), _intent(), _decision())
    record = store.load(inspection_id, SECOND_OPERATION_KEY)
    record.intent.prompt = "changed outside the store"

    assert inspection_id == FIRST_INSPECTION_ID
    assert record.schema_id == SCHEMA_ID
    assert USER_PROMPT not in repr(record)
    assert store.load(inspection_id, FIRST_OPERATION_KEY).intent.prompt == USER_PROMPT
    with pytest.raises(DashboardApiInspectionError, match="was not offered"):
        store.load(inspection_id, UNKNOWN_OPERATION_KEY)


def test_store_expires_and_evicts_inactive_inspections() -> None:
    now = [0.0]
    _iterator, token_factory = _tokens(
        FIRST_INSPECTION_ID,
        SECOND_INSPECTION_ID,
    )
    store = ApiInspectionStore(
        max_entries=1,
        ttl_seconds=TEST_TTL_SECONDS,
        clock=lambda: now[0],
        token_factory=token_factory,
    )
    first = store.put(_inspection(), _intent(), _decision())
    second = store.put(_inspection(), _intent(), _decision())

    with pytest.raises(DashboardApiInspectionError, match="expired"):
        store.load(first, FIRST_OPERATION_KEY)
    assert store.load(second, FIRST_OPERATION_KEY).schema_id == SCHEMA_ID
    now[0] = EXPIRED_TIME
    with pytest.raises(DashboardApiInspectionError, match="expired"):
        store.load(second, FIRST_OPERATION_KEY)


def test_store_rejects_invalid_bounds_content_identifiers_and_capacity() -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        ApiInspectionStore(max_entries=0)

    _iterator, token_factory = _tokens(FIRST_INSPECTION_ID)
    tiny = ApiInspectionStore(
        max_total_bytes=1,
        token_factory=token_factory,
    )
    with pytest.raises(DashboardApiInspectionError, match="too large"):
        tiny.put(_inspection(), _intent(), _decision())

    empty = _inspection().model_copy(update={"operations": [], "total_matches": 0})
    with pytest.raises(DashboardApiInspectionError, match="no selectable"):
        ApiInspectionStore().put(empty, _intent(), _decision())

    store = ApiInspectionStore()
    with pytest.raises(DashboardApiInspectionError, match="identifier is invalid"):
        store.load("invalid", FIRST_OPERATION_KEY)
    with pytest.raises(DashboardApiInspectionError, match="operation is invalid"):
        store.load(FIRST_INSPECTION_ID, "invalid")
    assert store.discard("invalid") is False
