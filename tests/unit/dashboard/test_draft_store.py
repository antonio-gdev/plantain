"""Bounded process-owned dashboard scenario-draft storage."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from plantain.dashboard.agent.models import AgentCapability, ScenarioDraft
from plantain.dashboard.draft_store import (
    DashboardDraftError,
    ScenarioDraftStore,
    is_valid_draft_id,
)

FIRST_DRAFT_ID = "a" * 32
SECOND_DRAFT_ID = "b" * 32
PAGE_CHARACTERS = 12
TEST_TTL_SECONDS = 10.0
EXPIRED_TIME = 11.0


def _draft(source: str = "scenario: Draft\nsteps:\n  - sendRequest: {}\n") -> ScenarioDraft:
    return ScenarioDraft(
        scenario="Draft",
        yaml_text=source,
        step_count=1,
        activities=["sendRequest"],
    )


def _tokens(*values: str) -> tuple[Iterator[str], object]:
    iterator = iter(values)

    def token_factory(_byte_count: int) -> str:
        return next(iterator)

    return iterator, token_factory


def test_store_pages_complete_content_and_returns_defensive_copy() -> None:
    source = "scenario: Draft\nsteps:\n  - sendRequest:\n      id: request\n"
    draft = _draft(source)
    expected = draft.yaml_text
    _iterator, token_factory = _tokens(FIRST_DRAFT_ID)
    store = ScenarioDraftStore(
        page_characters=PAGE_CHARACTERS,
        token_factory=token_factory,  # type: ignore[arg-type]
    )

    draft_id = store.put(draft, AgentCapability.API_CONTRACT)
    first = store.page(draft_id)
    assert first.suggested_directory is None
    pages = [store.page(draft_id, page) for page in range(1, first.page_count + 1)]
    record = store.load(draft_id)
    record.draft.yaml_text = "changed outside the store"

    assert draft_id == FIRST_DRAFT_ID
    assert is_valid_draft_id(draft_id) is True
    assert "".join(page.content for page in pages) == expected
    assert all(len(page.content) <= PAGE_CHARACTERS for page in pages)
    assert first.has_previous is False
    assert pages[-1].has_next is False
    assert source not in repr(first)
    assert store.load(draft_id).draft.yaml_text == expected


def test_store_expires_inactive_draft_without_exposing_content() -> None:
    now = [0.0]
    _iterator, token_factory = _tokens(FIRST_DRAFT_ID)
    store = ScenarioDraftStore(
        ttl_seconds=TEST_TTL_SECONDS,
        clock=lambda: now[0],
        token_factory=token_factory,  # type: ignore[arg-type]
    )
    draft_id = store.put(_draft(), AgentCapability.API_CONTRACT)
    now[0] = EXPIRED_TIME

    with pytest.raises(DashboardDraftError, match="expired") as captured:
        store.load(draft_id)

    assert "sendRequest" not in str(captured.value)


def test_store_evicts_oldest_draft_at_entry_capacity() -> None:
    _iterator, token_factory = _tokens(FIRST_DRAFT_ID, SECOND_DRAFT_ID)
    store = ScenarioDraftStore(
        max_entries=1,
        token_factory=token_factory,  # type: ignore[arg-type]
    )
    first = store.put(_draft("scenario: First\nsteps: [{}]\n"), AgentCapability.API_CONTRACT)
    second = store.put(
        _draft("scenario: Second\nsteps: [{}]\n"),
        AgentCapability.AUTOMATION_GENERATION,
    )

    with pytest.raises(DashboardDraftError, match="expired"):
        store.load(first)

    assert store.load(second).capability is AgentCapability.AUTOMATION_GENERATION
    assert store.discard(second) is True
    assert store.discard(second) is False


def test_store_rejects_invalid_bounds_identifiers_pages_and_oversize_drafts() -> None:
    with pytest.raises(ValueError, match="bounds must be positive"):
        ScenarioDraftStore(max_entries=0)

    _iterator, token_factory = _tokens("invalid")
    store = ScenarioDraftStore(
        max_total_bytes=4,
        token_factory=token_factory,  # type: ignore[arg-type]
    )
    with pytest.raises(DashboardDraftError, match="too large"):
        store.put(_draft(), AgentCapability.API_CONTRACT)

    assert is_valid_draft_id("invalid") is False
    assert store.discard("invalid") is False
    with pytest.raises(DashboardDraftError, match="page is invalid"):
        store.page(FIRST_DRAFT_ID, True)
