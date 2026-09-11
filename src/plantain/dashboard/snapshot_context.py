"""Intent-relevant verified snapshot evidence for dashboard agents."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, Self

from pydantic import Field, model_validator

from plantain.activities.snapshot_registry_complete import (
    CompleteSnapshotRegistry,
    VerifiedSnapshotExcerpt,
    VerifiedSnapshotSummary,
)
from plantain.activities.ui_errors import SnapshotError
from plantain.errors import PlantainError
from plantain.models.common import StrictModel
from plantain.security.secrets import SecretRegistry

MAX_SNAPSHOT_CONTEXT_EXCERPTS = 8
MAX_SNAPSHOT_CONTEXT_EXCERPT_BYTES = 8_192
MAX_SNAPSHOT_CONTEXT_TOTAL_BYTES = 49_152
MAX_SNAPSHOT_CONTEXT_ACTIVITIES = 32
MAX_SNAPSHOT_CONTEXT_KEY_IDS = 128
MAX_SNAPSHOT_CONTEXT_QUERY_TERMS = 32
MAX_SNAPSHOT_CONTEXT_SHORT_TEXT = 512
MAX_SNAPSHOT_CONTEXT_URL = 2_048
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOP_WORDS = frozenset(
    {
        "about",
        "and",
        "create",
        "derive",
        "for",
        "from",
        "plan",
        "should",
        "test",
        "testing",
        "that",
        "the",
        "this",
        "with",
    }
)
EvidenceId = Annotated[
    str,
    Field(min_length=29, max_length=29, pattern=r"^snapshot-[0-9a-f]{20}$"),
]
BoundedShortText = Annotated[
    str,
    Field(min_length=1, max_length=MAX_SNAPSHOT_CONTEXT_SHORT_TEXT),
]


class SnapshotContextError(PlantainError):
    """Raised when verified UI evidence cannot be prepared safely."""


class SnapshotElementCount(StrictModel):
    """One bounded structural count from a verified snapshot."""

    name: BoundedShortText
    count: int = Field(ge=0)


class SnapshotContextExcerpt(StrictModel):
    """One redacted backend-only semantic snapshot excerpt."""

    evidence_id: EvidenceId
    canonical_file: BoundedShortText
    activities: list[BoundedShortText] = Field(
        min_length=1,
        max_length=MAX_SNAPSHOT_CONTEXT_ACTIVITIES,
    )
    url: str = Field(min_length=1, max_length=MAX_SNAPSHOT_CONTEXT_URL)
    page_title: BoundedShortText
    normalized_key: str = Field(
        min_length=1,
        max_length=MAX_SNAPSHOT_CONTEXT_URL,
    )
    element_counts: list[SnapshotElementCount] = Field(
        default_factory=list,
        max_length=MAX_SNAPSHOT_CONTEXT_ACTIVITIES,
    )
    key_ids: list[BoundedShortText] = Field(
        default_factory=list,
        max_length=MAX_SNAPSHOT_CONTEXT_KEY_IDS,
    )
    content: str = Field(
        default="",
        max_length=MAX_SNAPSHOT_CONTEXT_EXCERPT_BYTES,
        repr=False,
    )
    truncated: bool = False


class SnapshotContextPacket(StrictModel):
    """Bounded verified UI evidence sent only to the configured provider."""

    source_count: int = Field(ge=0, le=MAX_SNAPSHOT_CONTEXT_EXCERPTS)
    excerpts: list[SnapshotContextExcerpt] = Field(
        default_factory=list,
        max_length=MAX_SNAPSHOT_CONTEXT_EXCERPTS,
    )
    selection_limited: bool = False

    @model_validator(mode="after")
    def source_count_matches_excerpts(self) -> Self:
        if self.source_count != len(self.excerpts):
            raise ValueError("Snapshot context source count must match its excerpts")
        return self


class _SnapshotRegistryReader(Protocol):
    def verified_summaries(self) -> tuple[VerifiedSnapshotSummary, ...]: ...

    def read_verified_excerpt(
        self,
        canonical_file: str,
        *,
        max_bytes: int,
    ) -> VerifiedSnapshotExcerpt: ...


@dataclass(frozen=True, slots=True)
class _Candidate:
    summary: VerifiedSnapshotSummary
    score: int


def select_verified_snapshot_context(
    snapshots_dir: Path,
    query: str,
    secrets: SecretRegistry,
    *,
    registry: _SnapshotRegistryReader | None = None,
) -> SnapshotContextPacket:
    """Select, verify, redact, and bound UI evidence for one agent request."""

    reader = registry or CompleteSnapshotRegistry(snapshots_dir)
    try:
        summaries = reader.verified_summaries()
        states, duplicates_collapsed = _latest_structural_states(summaries)
        ranked = _ranked_candidates(states, _query_terms(query))
        selected = ranked[:MAX_SNAPSHOT_CONTEXT_EXCERPTS]
        excerpts, content_limited = _read_selected(reader, selected, secrets)
    except (SnapshotError, RuntimeError, UnicodeError) as exc:
        raise SnapshotContextError("Verified UI evidence could not be prepared safely") from exc
    return SnapshotContextPacket(
        source_count=len(excerpts),
        excerpts=excerpts,
        selection_limited=(duplicates_collapsed or len(states) > len(selected) or content_limited),
    )


def _latest_structural_states(
    summaries: tuple[VerifiedSnapshotSummary, ...],
) -> tuple[tuple[VerifiedSnapshotSummary, ...], bool]:
    latest: dict[tuple[tuple[str, ...], str, str], VerifiedSnapshotSummary] = {}
    for summary in summaries:
        identity = (
            tuple(sorted(summary.activities)),
            summary.normalized_key,
            summary.structural_digest,
        )
        current = latest.get(identity)
        if current is None or (summary.last_updated, summary.canonical_file) > (
            current.last_updated,
            current.canonical_file,
        ):
            latest[identity] = summary
    return tuple(latest.values()), len(latest) != len(summaries)


def _ranked_candidates(
    summaries: tuple[VerifiedSnapshotSummary, ...],
    terms: tuple[str, ...],
) -> tuple[_Candidate, ...]:
    candidates = [
        _Candidate(summary=summary, score=_candidate_score(summary, terms)) for summary in summaries
    ]
    candidates.sort(
        key=lambda item: (
            item.summary.last_updated,
            item.summary.canonical_file,
        ),
        reverse=True,
    )
    candidates.sort(key=lambda item: item.score, reverse=True)
    return tuple(candidates)


def _candidate_score(
    summary: VerifiedSnapshotSummary,
    terms: tuple[str, ...],
) -> int:
    values = (
        *summary.activities,
        summary.url,
        summary.page_title,
        summary.normalized_key,
        *summary.key_ids,
    )
    haystack = "\n".join(values).casefold()
    return sum(1 for term in terms if term in haystack)


def _query_terms(query: str) -> tuple[str, ...]:
    values: dict[str, None] = {}
    for match in _TOKEN.finditer(query.casefold()):
        term = match.group()
        if term not in _STOP_WORDS:
            values.setdefault(term, None)
        if len(values) >= MAX_SNAPSHOT_CONTEXT_QUERY_TERMS:
            break
    return tuple(values)


def _read_selected(
    reader: _SnapshotRegistryReader,
    candidates: tuple[_Candidate, ...],
    secrets: SecretRegistry,
) -> tuple[list[SnapshotContextExcerpt], bool]:
    excerpts: list[SnapshotContextExcerpt] = []
    remaining_bytes = MAX_SNAPSHOT_CONTEXT_TOTAL_BYTES
    limited = False
    for candidate in candidates:
        if remaining_bytes <= 0:
            limited = True
            break
        raw = reader.read_verified_excerpt(
            candidate.summary.canonical_file,
            max_bytes=min(MAX_SNAPSHOT_CONTEXT_EXCERPT_BYTES, remaining_bytes),
        )
        excerpt, excerpt_limited = _redacted_excerpt(raw, secrets)
        excerpts.append(excerpt)
        remaining_bytes -= len(excerpt.content.encode())
        limited = limited or excerpt_limited
    return excerpts, limited


def _redacted_excerpt(
    raw: VerifiedSnapshotExcerpt,
    secrets: SecretRegistry,
) -> tuple[SnapshotContextExcerpt, bool]:
    summary = raw.summary
    canonical, canonical_limited = _bounded_text(
        secrets.redact_text(summary.canonical_file),
        MAX_SNAPSHOT_CONTEXT_SHORT_TEXT,
    )
    title, title_limited = _bounded_text(
        secrets.redact_text(summary.page_title),
        MAX_SNAPSHOT_CONTEXT_SHORT_TEXT,
    )
    normalized, normalized_limited = _bounded_text(
        secrets.redact_text(summary.normalized_key),
        MAX_SNAPSHOT_CONTEXT_URL,
    )
    url, url_limited = _bounded_text(
        secrets.redact_url(summary.url),
        MAX_SNAPSHOT_CONTEXT_URL,
    )
    activities, activities_limited = _bounded_values(
        summary.activities,
        secrets,
        MAX_SNAPSHOT_CONTEXT_ACTIVITIES,
    )
    key_ids, keys_limited = _bounded_values(
        summary.key_ids,
        secrets,
        MAX_SNAPSHOT_CONTEXT_KEY_IDS,
    )
    content, content_limited = _bounded_utf8(
        secrets.redact_multiline(raw.content),
        MAX_SNAPSHOT_CONTEXT_EXCERPT_BYTES,
    )
    counts = [
        SnapshotElementCount(
            name=_bounded_text(secrets.redact_text(name), MAX_SNAPSHOT_CONTEXT_SHORT_TEXT)[0],
            count=count,
        )
        for name, count in summary.element_counts
    ]
    limited = any(
        (
            raw.truncated,
            summary.summary_limited,
            canonical_limited,
            title_limited,
            normalized_limited,
            url_limited,
            activities_limited,
            keys_limited,
            content_limited,
        )
    )
    return (
        SnapshotContextExcerpt(
            evidence_id=_evidence_id(summary.canonical_file),
            canonical_file=canonical,
            activities=activities,
            url=url,
            page_title=title,
            normalized_key=normalized,
            element_counts=counts,
            key_ids=key_ids,
            content=content,
            truncated=limited,
        ),
        limited,
    )


def _bounded_values(
    values: tuple[str, ...],
    secrets: SecretRegistry,
    limit: int,
) -> tuple[list[str], bool]:
    retained: list[str] = []
    limited = len(values) > limit
    for value in values[:limit]:
        rendered, value_limited = _bounded_text(
            secrets.redact_text(value),
            MAX_SNAPSHOT_CONTEXT_SHORT_TEXT,
        )
        retained.append(rendered)
        limited = limited or value_limited
    return retained, limited


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _bounded_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value, False
    prefix = encoded[:limit]
    while prefix:
        try:
            return prefix.decode(), True
        except UnicodeDecodeError as exc:
            prefix = prefix[: exc.start]
    return "", True


def _evidence_id(canonical_file: str) -> str:
    digest = hashlib.sha256(canonical_file.encode()).hexdigest()[:20]
    return f"snapshot-{digest}"


__all__ = [
    "SnapshotContextError",
    "SnapshotContextExcerpt",
    "SnapshotContextPacket",
    "SnapshotElementCount",
    "select_verified_snapshot_context",
]
