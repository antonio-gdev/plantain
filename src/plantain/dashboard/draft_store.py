"""Bounded process-owned storage for reviewable dashboard scenario drafts."""

from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from plantain.dashboard.agent.models import AgentCapability, ScenarioDraft
from plantain.errors import PlantainError

DRAFT_ID_BYTES = 16
DRAFT_ID_GENERATION_ATTEMPTS = 8
DEFAULT_DRAFT_PAGE_CHARACTERS = 24_000
MAX_STORED_DRAFTS = 32
MAX_DRAFT_STORE_BYTES = 16_777_216
DRAFT_TTL_SECONDS = 3_600.0
_DRAFT_ID = re.compile(r"^[0-9a-f]{32}$")


class DashboardDraftError(PlantainError):
    """Raised when a dashboard-owned scenario draft is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class ScenarioDraftRecord:
    """Server-only draft record resolved through an opaque browser identifier."""

    draft_id: str
    capability: AgentCapability
    draft: ScenarioDraft = field(repr=False)


@dataclass(frozen=True, slots=True)
class ScenarioDraftPage:
    """One bounded, non-truncated page of a validated scenario draft."""

    draft_id: str
    capability: AgentCapability
    scenario: str
    step_count: int
    activities: tuple[str, ...]
    suggested_directory: str | None
    repaired: bool
    content: str = field(repr=False)
    page: int
    page_count: int
    has_previous: bool
    has_next: bool


@dataclass(frozen=True, slots=True)
class _StoredDraft:
    capability: AgentCapability
    draft: ScenarioDraft = field(repr=False)
    byte_count: int
    expires_at: float


class ScenarioDraftStore:
    """Retain active drafts within fixed process-memory and lifetime bounds."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_STORED_DRAFTS,
        max_total_bytes: int = MAX_DRAFT_STORE_BYTES,
        ttl_seconds: float = DRAFT_TTL_SECONDS,
        page_characters: int = DEFAULT_DRAFT_PAGE_CHARACTERS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        if max_entries <= 0 or max_total_bytes <= 0 or ttl_seconds <= 0 or page_characters <= 0:
            raise ValueError("Draft store bounds must be positive")
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._ttl_seconds = ttl_seconds
        self._page_characters = page_characters
        self._clock = clock
        self._token_factory = token_factory
        self._lock = Lock()
        self._entries: OrderedDict[str, _StoredDraft] = OrderedDict()
        self._total_bytes = 0

    def put(
        self,
        draft: ScenarioDraft,
        capability: AgentCapability,
    ) -> str:
        """Store one validated draft and return an opaque identifier."""

        retained = draft.model_copy(deep=True)
        byte_count = len(retained.yaml_text.encode())
        if byte_count > self._max_total_bytes:
            raise DashboardDraftError("The generated scenario is too large to review safely")
        with self._lock:
            now = self._clock()
            self._remove_expired(now)
            self._make_room(byte_count)
            draft_id = self._new_id()
            self._entries[draft_id] = _StoredDraft(
                capability=capability,
                draft=retained,
                byte_count=byte_count,
                expires_at=now + self._ttl_seconds,
            )
            self._total_bytes += byte_count
        return draft_id

    def load(self, draft_id: str) -> ScenarioDraftRecord:
        """Resolve and refresh one active draft without sharing mutable state."""

        if not is_valid_draft_id(draft_id):
            raise DashboardDraftError("The scenario draft identifier is invalid")
        with self._lock:
            now = self._clock()
            self._remove_expired(now)
            stored = self._entries.get(draft_id)
            if stored is None:
                raise DashboardDraftError(
                    "This scenario draft expired; create it again to continue"
                )
            refreshed = _StoredDraft(
                capability=stored.capability,
                draft=stored.draft,
                byte_count=stored.byte_count,
                expires_at=now + self._ttl_seconds,
            )
            self._entries[draft_id] = refreshed
            self._entries.move_to_end(draft_id)
            return ScenarioDraftRecord(
                draft_id=draft_id,
                capability=refreshed.capability,
                draft=refreshed.draft.model_copy(deep=True),
            )

    def page(self, draft_id: str, page: int = 1) -> ScenarioDraftPage:
        """Load one review page while preserving access to the complete draft."""

        if not isinstance(page, int) or isinstance(page, bool):
            raise DashboardDraftError("The scenario draft page is invalid")
        record = self.load(draft_id)
        source = record.draft.yaml_text
        page_count = max(
            1,
            (len(source) + self._page_characters - 1) // self._page_characters,
        )
        selected_page = min(max(page, 1), page_count)
        start = (selected_page - 1) * self._page_characters
        content = source[start : start + self._page_characters]
        return ScenarioDraftPage(
            draft_id=draft_id,
            capability=record.capability,
            scenario=record.draft.scenario,
            step_count=record.draft.step_count,
            activities=tuple(record.draft.activities),
            suggested_directory=record.draft.suggested_directory,
            repaired=record.draft.repaired,
            content=content,
            page=selected_page,
            page_count=page_count,
            has_previous=selected_page > 1,
            has_next=selected_page < page_count,
        )

    def discard(self, draft_id: str) -> bool:
        """Forget one draft after save, replacement, or explicit dismissal."""

        if not is_valid_draft_id(draft_id):
            return False
        with self._lock:
            return self._remove(draft_id)

    def _remove_expired(self, now: float) -> None:
        while self._entries:
            draft_id, stored = next(iter(self._entries.items()))
            if stored.expires_at > now:
                return
            self._remove(draft_id)

    def _make_room(self, byte_count: int) -> None:
        while self._entries and (
            len(self._entries) >= self._max_entries
            or self._total_bytes + byte_count > self._max_total_bytes
        ):
            oldest = next(iter(self._entries))
            self._remove(oldest)

    def _remove(self, draft_id: str) -> bool:
        stored = self._entries.pop(draft_id, None)
        if stored is None:
            return False
        self._total_bytes -= stored.byte_count
        return True

    def _new_id(self) -> str:
        for _attempt in range(DRAFT_ID_GENERATION_ATTEMPTS):
            candidate = self._token_factory(DRAFT_ID_BYTES)
            if _DRAFT_ID.fullmatch(candidate) is not None and candidate not in self._entries:
                return candidate
        raise DashboardDraftError("Plantain could not allocate a scenario draft safely")


_draft_store = ScenarioDraftStore()


def is_valid_draft_id(value: object) -> bool:
    """Return whether a browser value can identify a dashboard-owned draft."""

    return isinstance(value, str) and _DRAFT_ID.fullmatch(value) is not None


def store_scenario_draft(
    draft: ScenarioDraft,
    capability: AgentCapability,
) -> str:
    """Store a validated scenario in the process-owned dashboard draft store."""

    return _draft_store.put(draft, capability)


def load_scenario_draft(draft_id: str) -> ScenarioDraftRecord:
    """Load a complete draft for a backend-only save or validation operation."""

    return _draft_store.load(draft_id)


def load_scenario_draft_page(
    draft_id: str,
    page: int = 1,
) -> ScenarioDraftPage:
    """Load one bounded review page for browser state."""

    return _draft_store.page(draft_id, page)


def discard_scenario_draft(draft_id: str) -> bool:
    """Discard one dashboard-owned draft."""

    return _draft_store.discard(draft_id)


__all__ = [
    "DashboardDraftError",
    "ScenarioDraftPage",
    "ScenarioDraftRecord",
    "ScenarioDraftStore",
    "discard_scenario_draft",
    "is_valid_draft_id",
    "load_scenario_draft",
    "load_scenario_draft_page",
    "store_scenario_draft",
]
