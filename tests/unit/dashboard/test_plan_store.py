"""Bounded process-owned dashboard decision-plan storage coverage."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from plantain.dashboard.agent.models import (
    DecisionDimensionKind,
    DecisionEvidenceKind,
    DecisionEvidenceSource,
    DecisionPlanDimension,
    DecisionPlanDraft,
    DecisionPlanTest,
    DecisionPriority,
)
from plantain.dashboard.plan_store import (
    MAX_PLAN_SUMMARY_CHARACTERS,
    DashboardPlanError,
    DecisionPlanStore,
    is_valid_plan_id,
)

EXPECTED_PAGE_COUNT = 2
EXPIRED_TIME = 11.0
FIRST_PLAN_ID = "a" * 32
SECOND_PLAN_ID = "b" * 32
PLAN_TEST_PAGE_SIZE = 1
SNAPSHOT_EVIDENCE_ID = f"snapshot-{'c' * 20}"
SUMMARY_OVERFLOW_CHARACTERS = MAX_PLAN_SUMMARY_CHARACTERS + 20
TEST_TTL_SECONDS = 10.0


def _case(
    case_id: str,
    *,
    objective: str = "Verify the evidence-backed checkout path.",
) -> DecisionPlanTest:
    return DecisionPlanTest(
        case_id=case_id,
        title=case_id.replace("-", " ").title(),
        objective=objective,
        priority=DecisionPriority.CRITICAL,
        dimensions=[DecisionDimensionKind.CRITICAL_PATH],
        preconditions=["An item is available."],
        actions=["Follow the observed flow."],
        expected_results=["Checkout completes."],
        evidence_ids=[SNAPSHOT_EVIDENCE_ID],
    )


def _draft(
    *,
    feature: str = "Checkout",
    tests: list[DecisionPlanTest] | None = None,
) -> DecisionPlanDraft:
    return DecisionPlanDraft(
        feature=feature,
        sources=[
            DecisionEvidenceSource(
                evidence_id=SNAPSHOT_EVIDENCE_ID,
                kind=DecisionEvidenceKind.VERIFIED_UI,
                label="Checkout",
                reference="checkout.semantic.json",
            )
        ],
        assumptions=["The verified snapshot represents the current application."],
        gaps=[],
        dimensions=[
            DecisionPlanDimension(
                kind=DecisionDimensionKind.CRITICAL_PATH,
                summary="Cover the verified checkout path.",
                evidence_ids=[SNAPSHOT_EVIDENCE_ID],
            )
        ],
        tests=tests or [_case("checkout-path")],
    )


def _tokens(*values: str) -> tuple[Iterator[str], object]:
    iterator = iter(values)

    def token_factory(_byte_count: int) -> str:
        return next(iterator)

    return iterator, token_factory


def test_store_pages_summaries_loads_detail_and_returns_defensive_copy() -> None:
    long_objective = "x" * SUMMARY_OVERFLOW_CHARACTERS
    draft = _draft(
        tests=[
            _case("checkout-path", objective=long_objective),
            _case("checkout-boundary"),
        ]
    )
    _iterator, token_factory = _tokens(FIRST_PLAN_ID)
    store = DecisionPlanStore(
        page_size=PLAN_TEST_PAGE_SIZE,
        token_factory=token_factory,  # type: ignore[arg-type]
    )

    plan_id = store.put(draft)
    first = store.page(plan_id)
    second = store.page(plan_id, EXPECTED_PAGE_COUNT)
    detail = store.case(plan_id, "checkout-path")
    record = store.load(plan_id)
    record.draft.feature = "Changed outside the store"

    assert plan_id == FIRST_PLAN_ID
    assert is_valid_plan_id(plan_id) is True
    assert first.page_count == EXPECTED_PAGE_COUNT
    assert first.has_previous is False
    assert first.has_next is True
    assert second.has_previous is True
    assert second.has_next is False
    assert first.tests[0].summary_limited is True
    assert first.tests[0].objective.endswith("…")
    assert len(first.tests[0].objective) == MAX_PLAN_SUMMARY_CHARACTERS
    assert detail.objective == long_objective
    assert detail.actions == ("Follow the observed flow.",)
    assert long_objective not in repr(record)
    assert store.load(plan_id).draft.feature == "Checkout"


def test_store_expires_inactive_plan_without_exposing_content() -> None:
    now = [0.0]
    _iterator, token_factory = _tokens(FIRST_PLAN_ID)
    store = DecisionPlanStore(
        ttl_seconds=TEST_TTL_SECONDS,
        clock=lambda: now[0],
        token_factory=token_factory,  # type: ignore[arg-type]
    )
    plan_id = store.put(_draft())
    now[0] = EXPIRED_TIME

    with pytest.raises(DashboardPlanError, match="expired") as captured:
        store.load(plan_id)

    assert "evidence-backed checkout" not in str(captured.value)


def test_store_evicts_oldest_plan_at_entry_capacity() -> None:
    _iterator, token_factory = _tokens(FIRST_PLAN_ID, SECOND_PLAN_ID)
    store = DecisionPlanStore(
        max_entries=1,
        token_factory=token_factory,  # type: ignore[arg-type]
    )
    first = store.put(_draft(feature="First"))
    second = store.put(_draft(feature="Second"))

    with pytest.raises(DashboardPlanError, match="expired"):
        store.load(first)

    assert store.load(second).draft.feature == "Second"
    assert store.discard(second) is True
    assert store.discard(second) is False


def test_store_rejects_invalid_bounds_identifiers_pages_cases_and_size() -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        DecisionPlanStore(page_size=0)

    _iterator, token_factory = _tokens("invalid")
    store = DecisionPlanStore(
        max_total_bytes=4,
        token_factory=token_factory,  # type: ignore[arg-type]
    )
    with pytest.raises(DashboardPlanError, match="too large"):
        store.put(_draft())

    assert is_valid_plan_id("invalid") is False
    assert store.discard("invalid") is False
    with pytest.raises(DashboardPlanError, match="page is invalid"):
        store.page(FIRST_PLAN_ID, True)

    _iterator, valid_token_factory = _tokens(FIRST_PLAN_ID)
    valid_store = DecisionPlanStore(
        token_factory=valid_token_factory,  # type: ignore[arg-type]
    )
    plan_id = valid_store.put(_draft())
    with pytest.raises(DashboardPlanError, match="case identifier is invalid"):
        valid_store.case(plan_id, "../outside")
    with pytest.raises(DashboardPlanError, match="case is unavailable"):
        valid_store.case(plan_id, "missing-case")
