"""Bounded process-owned storage for reviewable critical-decision plans."""

from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from plantain.dashboard.agent.models import DecisionPlanDraft, DecisionPlanTest
from plantain.errors import PlantainError

PLAN_ID_BYTES = 16
PLAN_ID_GENERATION_ATTEMPTS = 8
DEFAULT_PLAN_TEST_PAGE_SIZE = 6
MAX_PLAN_SUMMARY_CHARACTERS = 480
MAX_STORED_PLANS = 32
MAX_PLAN_STORE_BYTES = 16_777_216
PLAN_TTL_SECONDS = 3_600.0
_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_CASE_ID = re.compile(r"^[a-z][a-z0-9-]{0,79}$")


class DashboardPlanError(PlantainError):
    """Raised when a dashboard-owned decision plan is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class DecisionPlanRecord:
    """Complete server-only plan resolved through an opaque browser identifier."""

    plan_id: str
    draft: DecisionPlanDraft = field(repr=False)


@dataclass(frozen=True, slots=True)
class PlanSourceView:
    """Browser-safe evidence provenance without source content."""

    evidence_id: str
    kind: str
    label: str
    reference: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class PlanDimensionView:
    """Browser-safe coverage-dimension summary."""

    kind: str
    summary: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanTestView:
    """Compact browser-safe test summary for one catalog page."""

    case_id: str
    title: str
    objective: str
    priority: str
    dimensions: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    summary_limited: bool


@dataclass(frozen=True, slots=True)
class PlanTestDetail:
    """Complete browser-safe detail loaded only for one selected case."""

    case_id: str
    title: str
    objective: str
    priority: str
    dimensions: tuple[str, ...]
    preconditions: tuple[str, ...]
    actions: tuple[str, ...]
    expected_results: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DecisionPlanPage:
    """One compact plan overview and one deterministic test page."""

    plan_id: str
    feature: str
    repaired: bool
    assumptions: tuple[str, ...]
    gaps: tuple[str, ...]
    sources: tuple[PlanSourceView, ...]
    dimensions: tuple[PlanDimensionView, ...]
    tests: tuple[PlanTestView, ...]
    test_count: int
    page: int
    page_count: int
    has_previous: bool
    has_next: bool


@dataclass(frozen=True, slots=True)
class _StoredPlan:
    draft: DecisionPlanDraft = field(repr=False)
    byte_count: int
    expires_at: float


class DecisionPlanStore:
    """Retain complete plans within fixed process-memory and lifetime bounds."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_STORED_PLANS,
        max_total_bytes: int = MAX_PLAN_STORE_BYTES,
        ttl_seconds: float = PLAN_TTL_SECONDS,
        page_size: int = DEFAULT_PLAN_TEST_PAGE_SIZE,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        if max_entries <= 0 or max_total_bytes <= 0 or ttl_seconds <= 0 or page_size <= 0:
            raise ValueError("Decision-plan store bounds must be positive")
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._ttl_seconds = ttl_seconds
        self._page_size = page_size
        self._clock = clock
        self._token_factory = token_factory
        self._lock = Lock()
        self._entries: OrderedDict[str, _StoredPlan] = OrderedDict()
        self._total_bytes = 0

    def put(self, draft: DecisionPlanDraft) -> str:
        """Store one validated plan and return an opaque identifier."""

        retained = draft.model_copy(deep=True)
        byte_count = len(retained.model_dump_json(by_alias=True).encode())
        if byte_count > self._max_total_bytes:
            raise DashboardPlanError("The generated plan is too large to review safely")
        with self._lock:
            now = self._clock()
            self._remove_expired(now)
            self._make_room(byte_count)
            plan_id = self._new_id()
            self._entries[plan_id] = _StoredPlan(
                draft=retained,
                byte_count=byte_count,
                expires_at=now + self._ttl_seconds,
            )
            self._total_bytes += byte_count
        return plan_id

    def load(self, plan_id: str) -> DecisionPlanRecord:
        """Resolve and refresh one plan without sharing mutable state."""

        if not is_valid_plan_id(plan_id):
            raise DashboardPlanError("The decision-plan identifier is invalid")
        with self._lock:
            now = self._clock()
            self._remove_expired(now)
            stored = self._entries.get(plan_id)
            if stored is None:
                raise DashboardPlanError("This decision plan expired; create it again to continue")
            refreshed = _StoredPlan(
                draft=stored.draft,
                byte_count=stored.byte_count,
                expires_at=now + self._ttl_seconds,
            )
            self._entries[plan_id] = refreshed
            self._entries.move_to_end(plan_id)
            return DecisionPlanRecord(
                plan_id=plan_id,
                draft=refreshed.draft.model_copy(deep=True),
            )

    def page(self, plan_id: str, page: int = 1) -> DecisionPlanPage:
        """Load a compact deterministic test page and full bounded overview."""

        if not isinstance(page, int) or isinstance(page, bool):
            raise DashboardPlanError("The decision-plan page is invalid")
        record = self.load(plan_id)
        draft = record.draft
        page_count = max(
            1,
            (len(draft.tests) + self._page_size - 1) // self._page_size,
        )
        selected_page = min(max(page, 1), page_count)
        start = (selected_page - 1) * self._page_size
        selected_tests = draft.tests[start : start + self._page_size]
        return DecisionPlanPage(
            plan_id=plan_id,
            feature=draft.feature,
            repaired=draft.repaired,
            assumptions=tuple(draft.assumptions),
            gaps=tuple(draft.gaps),
            sources=tuple(
                PlanSourceView(
                    evidence_id=item.evidence_id,
                    kind=item.kind.value,
                    label=item.label,
                    reference=item.reference,
                    truncated=item.truncated,
                )
                for item in draft.sources
            ),
            dimensions=tuple(
                PlanDimensionView(
                    kind=item.kind.value,
                    summary=item.summary,
                    evidence_ids=tuple(item.evidence_ids),
                )
                for item in draft.dimensions
            ),
            tests=tuple(_test_view(item) for item in selected_tests),
            test_count=len(draft.tests),
            page=selected_page,
            page_count=page_count,
            has_previous=selected_page > 1,
            has_next=selected_page < page_count,
        )

    def case(self, plan_id: str, case_id: str) -> PlanTestDetail:
        """Load complete detail for one selected case only."""

        if _CASE_ID.fullmatch(case_id) is None:
            raise DashboardPlanError("The decision-plan case identifier is invalid")
        record = self.load(plan_id)
        selected = next(
            (item for item in record.draft.tests if item.case_id == case_id),
            None,
        )
        if selected is None:
            raise DashboardPlanError("The selected decision-plan case is unavailable")
        return PlanTestDetail(
            case_id=selected.case_id,
            title=selected.title,
            objective=selected.objective,
            priority=selected.priority.value,
            dimensions=tuple(item.value for item in selected.dimensions),
            preconditions=tuple(selected.preconditions),
            actions=tuple(selected.actions),
            expected_results=tuple(selected.expected_results),
            evidence_ids=tuple(selected.evidence_ids),
        )

    def discard(self, plan_id: str) -> bool:
        """Forget one plan after save, replacement, or explicit dismissal."""

        if not is_valid_plan_id(plan_id):
            return False
        with self._lock:
            return self._remove(plan_id)

    def _remove_expired(self, now: float) -> None:
        while self._entries:
            plan_id, stored = next(iter(self._entries.items()))
            if stored.expires_at > now:
                return
            self._remove(plan_id)

    def _make_room(self, byte_count: int) -> None:
        while self._entries and (
            len(self._entries) >= self._max_entries
            or self._total_bytes + byte_count > self._max_total_bytes
        ):
            oldest = next(iter(self._entries))
            self._remove(oldest)

    def _remove(self, plan_id: str) -> bool:
        stored = self._entries.pop(plan_id, None)
        if stored is None:
            return False
        self._total_bytes -= stored.byte_count
        return True

    def _new_id(self) -> str:
        for _attempt in range(PLAN_ID_GENERATION_ATTEMPTS):
            candidate = self._token_factory(PLAN_ID_BYTES)
            if _PLAN_ID.fullmatch(candidate) is not None and candidate not in self._entries:
                return candidate
        raise DashboardPlanError("Plantain could not allocate a decision plan safely")


def _test_view(test: DecisionPlanTest) -> PlanTestView:
    objective = test.objective
    limited = len(objective) > MAX_PLAN_SUMMARY_CHARACTERS
    if limited:
        objective = f"{objective[: MAX_PLAN_SUMMARY_CHARACTERS - 1]}…"
    return PlanTestView(
        case_id=test.case_id,
        title=test.title,
        objective=objective,
        priority=test.priority.value,
        dimensions=tuple(item.value for item in test.dimensions),
        evidence_ids=tuple(test.evidence_ids),
        summary_limited=limited,
    )


_plan_store = DecisionPlanStore()


def is_valid_plan_id(value: object) -> bool:
    """Return whether a browser value can identify a dashboard-owned plan."""

    return isinstance(value, str) and _PLAN_ID.fullmatch(value) is not None


def store_decision_plan(draft: DecisionPlanDraft) -> str:
    """Store one validated decision plan in the process-owned store."""

    return _plan_store.put(draft)


def load_decision_plan(plan_id: str) -> DecisionPlanRecord:
    """Load a complete plan for backend-only persistence."""

    return _plan_store.load(plan_id)


def load_decision_plan_page(
    plan_id: str,
    page: int = 1,
) -> DecisionPlanPage:
    """Load one bounded plan review page for browser state."""

    return _plan_store.page(plan_id, page)


def load_decision_plan_case(
    plan_id: str,
    case_id: str,
) -> PlanTestDetail:
    """Load one selected test case for progressive disclosure."""

    return _plan_store.case(plan_id, case_id)


def discard_decision_plan(plan_id: str) -> bool:
    """Discard one dashboard-owned decision plan."""

    return _plan_store.discard(plan_id)


__all__ = [
    "DashboardPlanError",
    "DecisionPlanPage",
    "DecisionPlanRecord",
    "DecisionPlanStore",
    "PlanDimensionView",
    "PlanSourceView",
    "PlanTestDetail",
    "PlanTestView",
    "discard_decision_plan",
    "is_valid_plan_id",
    "load_decision_plan",
    "load_decision_plan_case",
    "load_decision_plan_page",
    "store_decision_plan",
]
