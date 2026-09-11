"""Bounded explicit projections of verified immutable run evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Generic, TypeVar

from plantain.dashboard.run_catalog import (
    RunCatalogError,
    VerifiedRunDocument,
    load_verified_run_document,
)
from plantain.security.redaction import RedactionPolicy

DEFAULT_EVIDENCE_PAGE_SIZE = 20
MAX_EVIDENCE_PAGE_SIZE = 50
MAX_EVIDENCE_ITEMS = 10_000
MAX_EVIDENCE_LABEL_LENGTH = 240
MAX_EVIDENCE_VALUE_CHARACTERS = 4_000
MAX_FAILURE_MESSAGE_CHARACTERS = 1_000
MAX_FAILURE_DETAILS = 20
MILLISECONDS_PER_SECOND = 1_000
TRUNCATION_SUFFIX = "… [display truncated]"
_STEP_STATUSES = frozenset({"passed", "failed"})
_FAILURE_DETAIL_KEYS = frozenset(
    {
        "diagnostic_snapshot",
        "diagnostic_status",
        "evidence_state",
        "failure_stage",
        "http_status",
        "schema_path",
        "schema_rule",
        "operation_index",
        "operation_total",
        "operation_type",
        "operation_target",
        "operation_error_type",
    }
)
T = TypeVar("T")


class RunEvidenceError(RuntimeError):
    """Raised when selected local evidence cannot be projected safely."""


@dataclass(frozen=True, slots=True)
class RunStepEvidence:
    """One bounded scenario-step result."""

    position: int
    activity: str
    step_id: str
    status: str
    duration: str


@dataclass(frozen=True, slots=True)
class RenderedEvidence:
    """One explicit structured evidence value and its display state."""

    text: str
    limited: bool


@dataclass(frozen=True, slots=True)
class RunOperationEvidence:
    """One ordered technical operation without unbounded raw values."""

    position: int
    domain: str
    phase: str
    activity: str
    step_id: str
    operation_type: str
    target: str
    status: str
    duration: str
    error_type: str
    operation_input: RenderedEvidence | None
    operation_expected: RenderedEvidence | None
    operation_actual: RenderedEvidence | None


@dataclass(frozen=True, slots=True)
class RunArtifactEvidence:
    """One safe relative artifact descriptor; content is not loaded here."""

    position: int
    kind: str
    path: str
    description: str


@dataclass(frozen=True, slots=True)
class RunFailureDetail:
    """One allowlisted structured failure detail."""

    label: str
    value: RenderedEvidence


@dataclass(frozen=True, slots=True)
class RunFailureEvidence:
    """Explicitly requested bounded failure evidence."""

    activity: str
    step_id: str
    error_type: str
    message: RenderedEvidence
    details: tuple[RunFailureDetail, ...]
    details_limited: bool


@dataclass(frozen=True, slots=True)
class EvidencePage(Generic[T]):
    """One page from a bounded ordered evidence collection."""

    items: tuple[T, ...]
    total_count: int
    total_count_limited: bool
    page: int
    page_count: int
    has_previous: bool
    has_next: bool
    notice: str


def load_run_steps(
    project_root: Path,
    run_id: str,
    *,
    page: int = 1,
    page_size: int = DEFAULT_EVIDENCE_PAGE_SIZE,
) -> EvidencePage[RunStepEvidence]:
    """Load one page of verified step results."""

    document = _verified_document(project_root, run_id)
    values, limited = _bounded_collection(document.value.get("steps"), "steps")
    items, invalid_count = _project_collection(
        values,
        lambda value, position: _step(value, position, document.redaction),
    )
    return _evidence_page(
        items,
        page=page,
        page_size=page_size,
        limited=limited,
        invalid_count=invalid_count,
        label="steps",
    )


def load_run_operations(
    project_root: Path,
    run_id: str,
    *,
    page: int = 1,
    page_size: int = DEFAULT_EVIDENCE_PAGE_SIZE,
) -> EvidencePage[RunOperationEvidence]:
    """Load one page of verified technical operations."""

    document = _verified_document(project_root, run_id)
    values, limited = _bounded_collection(
        document.value.get("operations"),
        "operations",
    )
    items, invalid_count = _project_collection(
        values,
        lambda value, position: _operation(value, position, document.redaction),
    )
    return _evidence_page(
        items,
        page=page,
        page_size=page_size,
        limited=limited,
        invalid_count=invalid_count,
        label="operations",
    )


def load_run_artifacts(
    project_root: Path,
    run_id: str,
    *,
    page: int = 1,
    page_size: int = DEFAULT_EVIDENCE_PAGE_SIZE,
) -> EvidencePage[RunArtifactEvidence]:
    """Load one page of artifact metadata without reading artifact content."""

    document = _verified_document(project_root, run_id)
    values, limited = _bounded_collection(
        document.value.get("artifacts"),
        "artifacts",
    )
    items, invalid_count = _project_collection(
        values,
        lambda value, position: _artifact(value, position, document.redaction),
    )
    return _evidence_page(
        items,
        page=page,
        page_size=page_size,
        limited=limited,
        invalid_count=invalid_count,
        label="artifacts",
    )


def load_run_failure(
    project_root: Path,
    run_id: str,
) -> RunFailureEvidence | None:
    """Load bounded failure evidence only after explicit selection."""

    document = _verified_document(project_root, run_id)
    value = document.value.get("failure")
    if value is None:
        return None
    failure = _failure(value, document.redaction)
    if failure is None:
        raise RunEvidenceError("The selected run failure evidence is invalid")
    return failure


def _verified_document(project_root: Path, run_id: str) -> VerifiedRunDocument:
    try:
        return load_verified_run_document(project_root, run_id)
    except RunCatalogError as exc:
        raise RunEvidenceError("The selected run evidence is unavailable") from exc


def _bounded_collection(value: Any, label: str) -> tuple[list[Any], bool]:
    if not isinstance(value, list):
        raise RunEvidenceError(f"The selected run {label} evidence is invalid")
    limited = len(value) > MAX_EVIDENCE_ITEMS
    return value[:MAX_EVIDENCE_ITEMS], limited


def _project_collection(
    values: list[Any],
    projector: Callable[[Any, int], T | None],
) -> tuple[tuple[T, ...], int]:
    items: list[T] = []
    invalid_count = 0
    for position, value in enumerate(values, start=1):
        item = projector(value, position)
        if item is None:
            invalid_count += 1
        else:
            items.append(item)
    return tuple(items), invalid_count


def _step(
    value: Any,
    position: int,
    redaction: RedactionPolicy,
) -> RunStepEvidence | None:
    if not isinstance(value, Mapping):
        return None
    activity = _safe_text(value.get("activity"), redaction)
    step_id = _safe_text(value.get("step_id"), redaction)
    status = value.get("status")
    duration_ms = _nonnegative_int(value.get("duration_ms"))
    if (
        activity is None
        or step_id is None
        or not isinstance(status, str)
        or status not in _STEP_STATUSES
        or duration_ms is None
    ):
        return None
    return RunStepEvidence(
        position=position,
        activity=activity,
        step_id=step_id,
        status=status,
        duration=_duration_label(duration_ms),
    )


def _operation(
    value: Any,
    position: int,
    redaction: RedactionPolicy,
) -> RunOperationEvidence | None:
    if not isinstance(value, Mapping):
        return None
    domain = _safe_text(value.get("domain"), redaction)
    phase = _safe_text(value.get("phase"), redaction)
    activity = _safe_text(value.get("activity"), redaction)
    step_id = _safe_text(value.get("step_id"), redaction)
    operation_type = _safe_text(value.get("operation_type"), redaction)
    target = _safe_text(value.get("operation_target"), redaction)
    status = _safe_text(value.get("status"), redaction)
    duration_ms = _nonnegative_int(value.get("duration_ms"))
    required = (domain, phase, activity, step_id, operation_type, target, status)
    if any(item is None for item in required) or duration_ms is None:
        return None
    return RunOperationEvidence(
        position=position,
        domain=domain or "",
        phase=phase or "",
        activity=activity or "",
        step_id=step_id or "",
        operation_type=operation_type or "",
        target=target or "",
        status=status or "",
        duration=_duration_label(duration_ms),
        error_type=_safe_text(value.get("operation_error_type"), redaction) or "",
        operation_input=_optional_evidence(value, "operation_input", redaction),
        operation_expected=_optional_evidence(
            value,
            "operation_expected",
            redaction,
        ),
        operation_actual=_optional_evidence(value, "operation_actual", redaction),
    )


def _artifact(
    value: Any,
    position: int,
    redaction: RedactionPolicy,
) -> RunArtifactEvidence | None:
    if not isinstance(value, Mapping):
        return None
    kind = _safe_text(value.get("kind"), redaction)
    path = _safe_artifact_path(value.get("path"), redaction)
    description = _safe_text(value.get("description"), redaction)
    if kind is None or path is None or description is None:
        return None
    return RunArtifactEvidence(
        position=position,
        kind=kind,
        path=path,
        description=description,
    )


def _failure(
    value: Any,
    redaction: RedactionPolicy,
) -> RunFailureEvidence | None:
    if not isinstance(value, Mapping):
        return None
    activity = _safe_text(value.get("activity"), redaction)
    step_id = _safe_text(value.get("step_id"), redaction)
    error_type = _safe_text(value.get("exception_type"), redaction)
    message = _render_message(value.get("message"), redaction)
    if activity is None or step_id is None or error_type is None or message is None:
        return None
    raw_details = value.get("details")
    if raw_details is not None and not isinstance(raw_details, Mapping):
        return None
    details = _failure_details(raw_details or {}, redaction)
    return RunFailureEvidence(
        activity=activity,
        step_id=step_id,
        error_type=error_type,
        message=message,
        details=details[:MAX_FAILURE_DETAILS],
        details_limited=len(details) > MAX_FAILURE_DETAILS,
    )


def _failure_details(
    value: Mapping[Any, Any],
    redaction: RedactionPolicy,
) -> tuple[RunFailureDetail, ...]:
    details: list[RunFailureDetail] = []
    keys = sorted(key for key in value if isinstance(key, str) and key in _FAILURE_DETAIL_KEYS)
    for key in keys:
        label = _safe_text(key.replace("_", " "), redaction)
        if label is not None:
            details.append(
                RunFailureDetail(
                    label=label,
                    value=_render_value(value[key], redaction),
                )
            )
    return tuple(details)


def _optional_evidence(
    value: Mapping[Any, Any],
    key: str,
    redaction: RedactionPolicy,
) -> RenderedEvidence | None:
    return _render_value(value[key], redaction) if key in value else None


def _render_value(value: Any, redaction: RedactionPolicy) -> RenderedEvidence:
    safe = redaction.redact_artifact(value)
    try:
        rendered = json.dumps(
            safe,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        rendered = '"<unavailable>"'
    if len(rendered) <= MAX_EVIDENCE_VALUE_CHARACTERS:
        return RenderedEvidence(text=rendered, limited=False)
    retained = MAX_EVIDENCE_VALUE_CHARACTERS - len(TRUNCATION_SUFFIX)
    return RenderedEvidence(
        text=f"{rendered[:retained]}{TRUNCATION_SUFFIX}",
        limited=True,
    )


def _render_message(
    value: Any,
    redaction: RedactionPolicy,
) -> RenderedEvidence | None:
    if not isinstance(value, str):
        return None
    rendered = " ".join(redaction.redact_text(value).split())
    if not rendered:
        return None
    if len(rendered) <= MAX_FAILURE_MESSAGE_CHARACTERS:
        return RenderedEvidence(text=rendered, limited=False)
    retained = MAX_FAILURE_MESSAGE_CHARACTERS - len(TRUNCATION_SUFFIX)
    return RenderedEvidence(
        text=f"{rendered[:retained]}{TRUNCATION_SUFFIX}",
        limited=True,
    )


def _safe_text(value: Any, redaction: RedactionPolicy) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = " ".join(redaction.redact_text(value).split())
    if not rendered:
        return None
    if len(rendered) <= MAX_EVIDENCE_LABEL_LENGTH:
        return rendered
    retained = MAX_EVIDENCE_LABEL_LENGTH - len(TRUNCATION_SUFFIX)
    return f"{rendered[:retained]}{TRUNCATION_SUFFIX}"


def _safe_artifact_path(
    value: Any,
    redaction: RedactionPolicy,
) -> str | None:
    rendered = _safe_text(value, redaction)
    if rendered is None or "\\" in rendered:
        return None
    candidate = PurePosixPath(rendered)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return rendered


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _duration_label(duration_ms: int) -> str:
    if duration_ms < MILLISECONDS_PER_SECOND:
        return f"{duration_ms} ms"
    return f"{duration_ms / MILLISECONDS_PER_SECOND:.2f} s"


def _evidence_page(
    items: tuple[T, ...],
    *,
    page: int,
    page_size: int,
    limited: bool,
    invalid_count: int,
    label: str,
) -> EvidencePage[T]:
    _validate_page_request(page, page_size)
    total_count = len(items)
    page_count = (total_count + page_size - 1) // page_size if total_count else 0
    effective_page = min(page, page_count) if page_count else 1
    start = (effective_page - 1) * page_size
    visible = items[start : start + page_size]
    return EvidencePage(
        items=visible,
        total_count=total_count,
        total_count_limited=limited,
        page=effective_page,
        page_count=page_count,
        has_previous=effective_page > 1,
        has_next=effective_page < page_count,
        notice=_evidence_notice(label, limited, invalid_count),
    )


def _validate_page_request(page: int, page_size: int) -> None:
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise RunEvidenceError("The evidence page must be a positive integer")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > MAX_EVIDENCE_PAGE_SIZE
    ):
        raise RunEvidenceError(
            f"The evidence page size must be between 1 and {MAX_EVIDENCE_PAGE_SIZE}"
        )


def _evidence_notice(
    label: str,
    limited: bool,
    invalid_count: int,
) -> str:
    messages: list[str] = []
    if limited:
        messages.append(
            f"This run contains more {label} than the viewer can retain at once; "
            "the displayed count is a lower bound."
        )
    if invalid_count:
        messages.append(
            f"{invalid_count} malformed "
            f"{label[:-1] if invalid_count == 1 else label} "
            f"{'entry was' if invalid_count == 1 else 'entries were'} excluded."
        )
    return " ".join(messages)


__all__ = [
    "DEFAULT_EVIDENCE_PAGE_SIZE",
    "EvidencePage",
    "RenderedEvidence",
    "RunArtifactEvidence",
    "RunEvidenceError",
    "RunFailureDetail",
    "RunFailureEvidence",
    "RunOperationEvidence",
    "RunStepEvidence",
    "load_run_artifacts",
    "load_run_failure",
    "load_run_operations",
    "load_run_steps",
]
