"""Intent-relevant, redacted context selection for dashboard agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated

from pydantic import Field

from plantain.dashboard.context_sources import (
    MAX_RELATIVE_PATH_LENGTH,
    MAX_SOURCE_LABEL_LENGTH,
    ContextSourceDescriptor,
    ContextSourceKind,
    load_context_descriptors,
)
from plantain.errors import AtomicPersistenceError, PlantainError
from plantain.models.common import StrictModel
from plantain.persistence import open_binary_read_no_follow
from plantain.security.secrets import SecretRegistry

MAX_AGENT_CONTEXT_EXCERPTS = 12
MAX_AGENT_CONTEXT_EXCERPT_BYTES = 8_192
MAX_AGENT_CONTEXT_TOTAL_BYTES = 49_152
MAX_CONTEXT_CANDIDATES_PER_SOURCE = 24
MAX_CONTEXT_QUERY_TERMS = 32
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "also",
        "and",
        "app",
        "application",
        "before",
        "create",
        "from",
        "help",
        "into",
        "need",
        "should",
        "test",
        "testing",
        "that",
        "the",
        "their",
        "then",
        "this",
        "want",
        "with",
    }
)
_KIND_PRIORITY = {
    ContextSourceKind.REQUIREMENTS: 300,
    ContextSourceKind.APPLICATION: 200,
    ContextSourceKind.API_CONTRACT: 100,
}
_API_TERMS = frozenset({"api", "contract", "endpoint", "http", "openapi", "schema", "swagger"})
_APPLICATION_TERMS = frozenset(
    {"behavior", "checkout", "code", "flow", "project", "source", "ui", "web"}
)
_REQUIREMENT_TERMS = frozenset(
    {"acceptance", "business", "criteria", "requirement", "rule", "specification"}
)
BoundedContextLabel = Annotated[
    str,
    Field(min_length=1, max_length=MAX_SOURCE_LABEL_LENGTH),
]
BoundedContextPath = Annotated[
    str,
    Field(min_length=1, max_length=MAX_RELATIVE_PATH_LENGTH),
]


class ContextSelectionError(PlantainError):
    """Raised when selected context cannot be prepared safely."""


class AgentContextExcerpt(StrictModel):
    """One sanitized source excerpt supplied only to the backend agent."""

    source_kind: ContextSourceKind
    source_label: BoundedContextLabel
    relative_path: BoundedContextPath
    content: str = Field(
        min_length=1,
        max_length=MAX_AGENT_CONTEXT_EXCERPT_BYTES,
    )
    truncated: bool = False


class AgentContextPacket(StrictModel):
    """Bounded context selected for one ephemeral agent request."""

    source_count: int = Field(ge=0, le=MAX_AGENT_CONTEXT_EXCERPTS)
    excerpts: list[AgentContextExcerpt] = Field(
        default_factory=list,
        max_length=MAX_AGENT_CONTEXT_EXCERPTS,
    )
    selection_limited: bool = False


@dataclass(frozen=True, slots=True)
class ContextSelection:
    """Backend-only packet plus the source kinds it actually represents."""

    packet: AgentContextPacket
    available_kinds: frozenset[ContextSourceKind]


@dataclass(frozen=True, slots=True)
class _Candidate:
    descriptor: ContextSourceDescriptor
    relative_path: str
    score: int


@dataclass(frozen=True, slots=True)
class _ReadResult:
    excerpt: AgentContextExcerpt
    byte_count: int
    limited: bool


def select_agent_context(
    project_root: Path,
    query: str,
    secrets: SecretRegistry,
) -> ContextSelection:
    """Select and redact a bounded evidence packet for one user request."""

    descriptors = load_context_descriptors(project_root)
    candidates = _ranked_candidates(descriptors, _query_terms(query))
    excerpts: list[AgentContextExcerpt] = []
    represented: set[str] = set()
    available_kinds: set[ContextSourceKind] = set()
    remaining_bytes = MAX_AGENT_CONTEXT_TOTAL_BYTES
    limited = any(descriptor.partial for descriptor in descriptors)
    for candidate in candidates:
        if len(excerpts) >= MAX_AGENT_CONTEXT_EXCERPTS:
            limited = True
            break
        if remaining_bytes <= 0:
            limited = True
            break
        result = _read_candidate(
            candidate,
            min(MAX_AGENT_CONTEXT_EXCERPT_BYTES, remaining_bytes),
            secrets,
        )
        if result is None:
            limited = True
            continue
        excerpts.append(result.excerpt)
        represented.add(candidate.descriptor.source_id)
        available_kinds.add(candidate.descriptor.kind)
        remaining_bytes -= result.byte_count
        limited = limited or result.limited
    return ContextSelection(
        packet=AgentContextPacket(
            source_count=len(represented),
            excerpts=excerpts,
            selection_limited=limited,
        ),
        available_kinds=frozenset(available_kinds),
    )


def _ranked_candidates(
    descriptors: tuple[ContextSourceDescriptor, ...],
    terms: tuple[str, ...],
) -> tuple[_Candidate, ...]:
    heads: list[_Candidate] = []
    remaining: list[_Candidate] = []
    for descriptor in descriptors:
        ranked = sorted(
            (
                _Candidate(
                    descriptor=descriptor,
                    relative_path=relative_path,
                    score=_candidate_score(descriptor, relative_path, terms),
                )
                for relative_path in descriptor.files
            ),
            key=_candidate_order,
        )[:MAX_CONTEXT_CANDIDATES_PER_SOURCE]
        if ranked:
            heads.append(ranked[0])
            remaining.extend(ranked[1:])
    heads.sort(key=_candidate_order)
    remaining.sort(key=_candidate_order)
    return (*heads, *remaining)


def _candidate_order(candidate: _Candidate) -> tuple[int, str, str]:
    return (
        -candidate.score,
        candidate.relative_path.casefold(),
        candidate.relative_path,
    )


def _candidate_score(
    descriptor: ContextSourceDescriptor,
    relative_path: str,
    terms: tuple[str, ...],
) -> int:
    folded = relative_path.casefold()
    name = PurePosixPath(relative_path).name.casefold()
    score = _KIND_PRIORITY[descriptor.kind]
    score += sum(40 for term in terms if term in folded)
    if name.startswith("readme"):
        score += 120
    if "requirement" in name or name.endswith(".feature"):
        score += 100
    if "openapi" in name or "swagger" in name:
        score += 100
    if "test" in PurePosixPath(relative_path).parts:
        score += 30
    term_set = frozenset(terms)
    if descriptor.kind is ContextSourceKind.API_CONTRACT:
        score += 240 * bool(term_set & _API_TERMS)
    elif descriptor.kind is ContextSourceKind.APPLICATION:
        score += 160 * bool(term_set & _APPLICATION_TERMS)
    elif descriptor.kind is ContextSourceKind.REQUIREMENTS:
        score += 200 * bool(term_set & _REQUIREMENT_TERMS)
    return score


def _query_terms(query: str) -> tuple[str, ...]:
    ordered: dict[str, None] = {}
    for match in _TOKEN.finditer(query.casefold()):
        token = match.group()
        if token not in _STOP_WORDS:
            ordered.setdefault(token, None)
        if len(ordered) >= MAX_CONTEXT_QUERY_TERMS:
            break
    return tuple(ordered)


def _read_candidate(
    candidate: _Candidate,
    byte_limit: int,
    secrets: SecretRegistry,
) -> _ReadResult | None:
    target = _candidate_path(candidate)
    try:
        with open_binary_read_no_follow(target) as stream:
            raw = stream.read(byte_limit + 1)
    except (AtomicPersistenceError, OSError):
        return None
    if not raw or b"\0" in raw:
        return None
    limited = len(raw) > byte_limit
    content = _decode_prefix(raw[:byte_limit], limited=limited)
    if content is None or not content.strip():
        return None
    try:
        redacted = secrets.redact_multiline(content)
    except RuntimeError as exc:
        raise ContextSelectionError("Selected context could not be redacted safely.") from exc
    redacted, redaction_limited = _bounded_text(redacted, byte_limit)
    if not redacted.strip():
        return None
    encoded = redacted.encode()
    excerpt = AgentContextExcerpt(
        source_kind=candidate.descriptor.kind,
        source_label=candidate.descriptor.label,
        relative_path=candidate.relative_path,
        content=redacted,
        truncated=limited or redaction_limited,
    )
    return _ReadResult(
        excerpt=excerpt,
        byte_count=len(encoded),
        limited=excerpt.truncated,
    )


def _candidate_path(candidate: _Candidate) -> Path:
    descriptor = candidate.descriptor
    if not descriptor.is_directory:
        return descriptor.path
    relative = PurePosixPath(candidate.relative_path)
    return descriptor.path.joinpath(*relative.parts)


def _decode_prefix(payload: bytes, *, limited: bool) -> str | None:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        if limited and exc.end == len(payload):
            return payload[: exc.start].decode("utf-8")
        return None


def _bounded_text(value: str, byte_limit: int) -> tuple[str, bool]:
    encoded = value.encode()
    if len(encoded) <= byte_limit:
        return value, False
    return encoded[:byte_limit].decode("utf-8", errors="ignore"), True


__all__ = [
    "AgentContextExcerpt",
    "AgentContextPacket",
    "ContextSelection",
    "ContextSelectionError",
    "select_agent_context",
]
