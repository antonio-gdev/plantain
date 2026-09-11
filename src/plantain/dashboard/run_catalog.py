"""Bounded, browser-safe catalog and metadata detail for immutable native runs."""

from __future__ import annotations

import hmac
import json
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from plantain.config import Settings
from plantain.engine.report_store import is_valid_report_run_id
from plantain.errors import AtomicPersistenceError, ConfigurationError
from plantain.models.scenario import MAX_SCENARIO_STEPS
from plantain.persistence import open_binary_read_no_follow
from plantain.security.redaction import RedactionPolicy

DEFAULT_RUN_PAGE_SIZE = 20
MAX_RUN_PAGE_SIZE = 50
MAX_RUN_CATALOG_ENTRIES = 10_000
MAX_RUN_CATALOG_READ_BYTES = 33_554_432
MAX_RUN_REPORT_BYTES = 4_194_304
MAX_RUN_SEARCH_LENGTH = 200
MAX_RUN_LABEL_LENGTH = 160
MAX_RUN_TAGS = 100
MAX_DISPLAY_TAGS = 3
MAX_DISPLAY_INTEGRATIONS = 12
MILLISECONDS_PER_SECOND = 1_000
RUN_RESULT_SUFFIX = ".result.json"
RunStatus = Literal["passed", "failed", "cancelled"]
_RUN_STATUSES = frozenset({"passed", "failed", "cancelled"})
_RUN_FILTERS = frozenset({"all", *_RUN_STATUSES})
_STEP_STATUSES = frozenset({"passed", "failed"})


class RunCatalogError(RuntimeError):
    """Raised when local native results cannot be projected safely."""


@dataclass(frozen=True, slots=True)
class VerifiedRunDocument:
    """Backend-only verified result document; never serialize this object to Reflex."""

    value: Mapping[str, Any]
    redaction: RedactionPolicy


@dataclass(frozen=True, slots=True)
class RunFailureSummary:
    """Bounded structured failure attribution without an arbitrary message."""

    activity: str
    step_id: str
    error_type: str


@dataclass(frozen=True, slots=True)
class RunIntegrationSummary:
    """One optional integration's bounded terminal state."""

    provider: str
    status: str


@dataclass(frozen=True, slots=True)
class RunCatalogItem:
    """Browser-safe summary for one immutable native result."""

    run_id: str
    scenario: str
    status: RunStatus
    source: str
    started: str
    duration: str
    step_count: int
    passed_step_count: int
    failed_step_count: int
    tags: tuple[str, ...]
    additional_tag_count: int
    integration_count: int
    failure: RunFailureSummary | None


@dataclass(frozen=True, slots=True)
class RunDetail:
    """Metadata-only run detail; heavy evidence is loaded by dedicated inspectors."""

    summary: RunCatalogItem
    jira_ticket: str
    test_case_key: str
    test_run_key: str
    integrations: tuple[RunIntegrationSummary, ...]
    additional_integration_count: int


@dataclass(frozen=True, slots=True)
class RunCatalogPage:
    """One deterministic page from a bounded immutable-result scan."""

    items: tuple[RunCatalogItem, ...]
    total_count: int
    total_count_limited: bool
    page: int
    page_count: int
    has_previous: bool
    has_next: bool
    notice: str


@dataclass(frozen=True, slots=True)
class _ReportCandidate:
    path: Path
    run_id: str
    relative_path: str
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class _ReportCollection:
    candidates: tuple[_ReportCandidate, ...]
    invalid_count: int
    limited: bool


@dataclass(frozen=True, slots=True)
class _ReportRead:
    value: dict[str, Any] | None
    byte_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class _RunProjection:
    item: RunCatalogItem
    detail: RunDetail


@dataclass(frozen=True, slots=True)
class _ScannedRuns:
    projections: tuple[_RunProjection, ...]
    invalid_count: int
    limited: bool


def load_run_catalog(
    project_root: Path,
    *,
    page: int = 1,
    page_size: int = DEFAULT_RUN_PAGE_SIZE,
    status: str = "all",
    query: str = "",
) -> RunCatalogPage:
    """Load one filtered page without retaining raw report payloads."""

    normalized_status, normalized_query = _validate_catalog_request(
        page,
        page_size,
        status,
        query,
    )
    settings, collection = _catalog_inputs(project_root)
    scanned = _scan_reports(
        collection,
        RedactionPolicy(settings.sensitive_key_names),
    )
    matching = tuple(
        projection.item
        for projection in scanned.projections
        if _matches(projection.item, normalized_status, normalized_query)
    )
    total_count = len(matching)
    page_count = (total_count + page_size - 1) // page_size if total_count else 0
    effective_page = min(page, page_count) if page_count else 1
    start = (effective_page - 1) * page_size
    visible = matching[start : start + page_size]
    return RunCatalogPage(
        items=visible,
        total_count=total_count,
        total_count_limited=scanned.limited,
        page=effective_page,
        page_count=page_count,
        has_previous=effective_page > 1,
        has_next=effective_page < page_count,
        notice=_catalog_notice(
            start=start,
            visible_count=len(visible),
            total_count=total_count,
            invalid_count=scanned.invalid_count,
            limited=scanned.limited,
        ),
    )


def load_run_detail(project_root: Path, run_id: str) -> RunDetail:
    """Resolve one opaque run ID and return metadata-only detail."""

    document, candidate = _selected_run_document(project_root, run_id)
    projection = _project_report(
        document.value,
        candidate,
        document.redaction,
    )
    if projection is None:
        raise RunCatalogError("The selected run result is invalid")
    return projection.detail


def load_verified_run_document(
    project_root: Path,
    run_id: str,
) -> VerifiedRunDocument:
    """Load one verified document for backend-only bounded evidence projection."""

    document, _candidate = _selected_run_document(project_root, run_id)
    return document


def _selected_run_document(
    project_root: Path,
    run_id: str,
) -> tuple[VerifiedRunDocument, _ReportCandidate]:
    if not is_valid_report_run_id(run_id):
        raise RunCatalogError("The selected run identifier is invalid")
    settings, collection = _catalog_inputs(project_root)
    matches = tuple(
        candidate
        for candidate in collection.candidates
        if hmac.compare_digest(candidate.run_id, run_id)
    )
    if len(matches) != 1:
        raise RunCatalogError("The selected run is no longer available")
    report = _read_report(matches[0].path, MAX_RUN_REPORT_BYTES)
    if report.truncated or report.value is None:
        raise RunCatalogError("The selected run result is invalid")
    correlation_id = report.value.get("correlation_id")
    if not isinstance(correlation_id, str) or not hmac.compare_digest(
        correlation_id,
        run_id,
    ):
        raise RunCatalogError("The selected run result is invalid")
    return (
        VerifiedRunDocument(
            value=MappingProxyType(report.value),
            redaction=RedactionPolicy(settings.sensitive_key_names),
        ),
        matches[0],
    )


def _validate_catalog_request(
    page: int,
    page_size: int,
    status: str,
    query: str,
) -> tuple[str, str]:
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise RunCatalogError("The run catalog page must be a positive integer")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > MAX_RUN_PAGE_SIZE
    ):
        raise RunCatalogError(
            f"The run catalog page size must be between 1 and {MAX_RUN_PAGE_SIZE}"
        )
    if not isinstance(status, str) or status.lower() not in _RUN_FILTERS:
        raise RunCatalogError("The run status filter is invalid")
    if not isinstance(query, str) or len(query) > MAX_RUN_SEARCH_LENGTH:
        raise RunCatalogError("The run search query is invalid")
    return status.lower(), " ".join(query.casefold().split())


def _catalog_inputs(project_root: Path) -> tuple[Settings, _ReportCollection]:
    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise RunCatalogError("The dashboard workspace is unavailable") from exc
    if not root.is_dir():
        raise RunCatalogError("The dashboard workspace is unavailable")
    try:
        settings = Settings.from_env(root)
        collection = _discover_reports(settings.output_dir / "results")
    except (ConfigurationError, OSError) as exc:
        raise RunCatalogError("The local run collection is unavailable") from exc
    return settings, collection


def _discover_reports(directory: Path) -> _ReportCollection:
    absolute = _validated_report_root(directory)
    if absolute is None:
        return _ReportCollection(candidates=(), invalid_count=0, limited=False)
    pending = [absolute]
    candidates: list[_ReportCandidate] = []
    invalid_count = 0
    inspected = 0
    limited = False
    while pending and not limited:
        current = pending.pop()
        entries = _directory_entries(current)
        for entry in entries:
            if inspected >= MAX_RUN_CATALOG_ENTRIES:
                limited = True
                break
            inspected += 1
            if entry.is_symlink():
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False) and entry.name.endswith(RUN_RESULT_SUFFIX):
                candidate = _report_candidate(path, absolute)
                if candidate is None:
                    invalid_count += 1
                else:
                    candidates.append(candidate)
    return _unique_candidates(candidates, invalid_count, limited)


def _validated_report_root(directory: Path) -> Path | None:
    if directory.is_symlink():
        raise RunCatalogError("The local run collection has an invalid location")
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise RunCatalogError("The local run collection has an invalid location")
    try:
        absolute = directory.absolute()
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise RunCatalogError("The local run collection has an invalid location") from exc
    if resolved != absolute:
        raise RunCatalogError("The local run collection has an invalid location")
    return absolute


def _directory_entries(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as scanner:
            return sorted(scanner, key=lambda entry: entry.name)
    except OSError as exc:
        raise RunCatalogError("The local run collection could not be read safely") from exc


def _report_candidate(path: Path, root: Path) -> _ReportCandidate | None:
    run_id = path.name.removesuffix(RUN_RESULT_SUFFIX)
    if not is_valid_report_run_id(run_id):
        return None
    try:
        metadata = path.stat(follow_symlinks=False)
        relative = path.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
    return _ReportCandidate(
        path=path,
        run_id=run_id,
        relative_path=relative,
        size=max(0, metadata.st_size),
        modified_ns=metadata.st_mtime_ns,
    )


def _unique_candidates(
    candidates: list[_ReportCandidate],
    invalid_count: int,
    limited: bool,
) -> _ReportCollection:
    counts = Counter(candidate.run_id for candidate in candidates)
    ambiguous = {run_id for run_id, count in counts.items() if count > 1}
    retained = [candidate for candidate in candidates if candidate.run_id not in ambiguous]
    invalid_count += sum(counts[run_id] for run_id in ambiguous)
    retained.sort(key=lambda candidate: (-candidate.modified_ns, candidate.relative_path))
    return _ReportCollection(
        candidates=tuple(retained),
        invalid_count=invalid_count,
        limited=limited,
    )


def _scan_reports(
    collection: _ReportCollection,
    redaction: RedactionPolicy,
) -> _ScannedRuns:
    projections: list[_RunProjection] = []
    invalid_count = collection.invalid_count
    consumed_bytes = 0
    limited = collection.limited
    for candidate in collection.candidates:
        remaining = MAX_RUN_CATALOG_READ_BYTES - consumed_bytes
        if remaining <= 0 or candidate.size > remaining:
            limited = True
            break
        if candidate.size > MAX_RUN_REPORT_BYTES:
            invalid_count += 1
            continue
        report = _read_report(
            candidate.path,
            min(MAX_RUN_REPORT_BYTES, remaining),
        )
        consumed_bytes += min(report.byte_count, remaining)
        if report.truncated:
            if remaining < MAX_RUN_REPORT_BYTES:
                limited = True
                break
            invalid_count += 1
            continue
        if report.value is None:
            invalid_count += 1
            continue
        projection = _project_report(report.value, candidate, redaction)
        if projection is None:
            invalid_count += 1
        else:
            projections.append(projection)
    return _ScannedRuns(
        projections=tuple(projections),
        invalid_count=invalid_count,
        limited=limited,
    )


def _read_report(path: Path, byte_limit: int) -> _ReportRead:
    try:
        with open_binary_read_no_follow(path, private=True) as stream:
            raw = stream.read(byte_limit + 1)
    except (AtomicPersistenceError, OSError):
        return _ReportRead(value=None, byte_count=0, truncated=False)
    if len(raw) > byte_limit:
        return _ReportRead(value=None, byte_count=len(raw), truncated=True)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return _ReportRead(value=None, byte_count=len(raw), truncated=False)
    if not isinstance(value, dict):
        return _ReportRead(value=None, byte_count=len(raw), truncated=False)
    return _ReportRead(
        value=cast("dict[str, Any]", value),
        byte_count=len(raw),
        truncated=False,
    )


def _project_report(
    value: Mapping[str, Any],
    candidate: _ReportCandidate,
    redaction: RedactionPolicy,
) -> _RunProjection | None:
    correlation_id = value.get("correlation_id")
    scenario = _safe_text(value.get("scenario"), redaction)
    status = _run_status(value.get("status"))
    duration_ms = _nonnegative_int(value.get("duration_ms"))
    started_at_ms = _nonnegative_int(value.get("started_at_ms"))
    step_counts = _step_counts(value.get("steps"))
    tags = _safe_tags(value.get("tags"), redaction)
    if (
        not isinstance(correlation_id, str)
        or not hmac.compare_digest(correlation_id, candidate.run_id)
        or scenario is None
        or status is None
        or duration_ms is None
        or started_at_ms is None
        or step_counts is None
        or tags is None
    ):
        return None
    failure = _failure_summary(value.get("failure"), redaction)
    integrations = _integration_summaries(value.get("integrations"), redaction)
    visible_integrations = integrations[:MAX_DISPLAY_INTEGRATIONS]
    visible_tags = tags[:MAX_DISPLAY_TAGS]
    item = RunCatalogItem(
        run_id=candidate.run_id,
        scenario=scenario,
        status=status,
        source=_source_label(value.get("source_path"), redaction),
        started=_started_label(started_at_ms, candidate),
        duration=_duration_label(duration_ms),
        step_count=sum(step_counts),
        passed_step_count=step_counts[0],
        failed_step_count=step_counts[1],
        tags=visible_tags,
        additional_tag_count=len(tags) - len(visible_tags),
        integration_count=len(integrations),
        failure=failure,
    )
    detail = RunDetail(
        summary=item,
        jira_ticket=_metadata_label(value.get("jira_ticket"), redaction),
        test_case_key=_metadata_label(value.get("test_case_key"), redaction),
        test_run_key=_metadata_label(value.get("test_run_key"), redaction),
        integrations=visible_integrations,
        additional_integration_count=len(integrations) - len(visible_integrations),
    )
    return _RunProjection(item=item, detail=detail)


def _run_status(value: Any) -> RunStatus | None:
    if not isinstance(value, str) or value.lower() not in _RUN_STATUSES:
        return None
    return cast("RunStatus", value.lower())


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _step_counts(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) > MAX_SCENARIO_STEPS:
        return None
    passed = 0
    failed = 0
    for step in value:
        if not isinstance(step, dict) or step.get("status") not in _STEP_STATUSES:
            return None
        if _nonnegative_int(step.get("duration_ms")) is None:
            return None
        if not isinstance(step.get("activity"), str) or not isinstance(
            step.get("step_id"),
            str,
        ):
            return None
        if step["status"] == "passed":
            passed += 1
        else:
            failed += 1
    return passed, failed


def _safe_tags(
    value: Any,
    redaction: RedactionPolicy,
) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > MAX_RUN_TAGS:
        return None
    rendered: list[str] = []
    for tag in value:
        safe = _safe_text(tag, redaction)
        if safe is None:
            return None
        rendered.append(safe)
    return tuple(rendered)


def _failure_summary(
    value: Any,
    redaction: RedactionPolicy,
) -> RunFailureSummary | None:
    if not isinstance(value, dict):
        return None
    activity = _safe_text(value.get("activity"), redaction)
    step_id = _safe_text(value.get("step_id"), redaction)
    error_type = _safe_text(value.get("exception_type"), redaction)
    if activity is None or step_id is None or error_type is None:
        return None
    return RunFailureSummary(
        activity=activity,
        step_id=step_id,
        error_type=error_type,
    )


def _integration_summaries(
    value: Any,
    redaction: RedactionPolicy,
) -> tuple[RunIntegrationSummary, ...]:
    if not isinstance(value, dict):
        return ()
    summaries: list[RunIntegrationSummary] = []
    for raw_provider in sorted(key for key in value if isinstance(key, str)):
        raw_state = value[raw_provider]
        if not isinstance(raw_state, dict):
            continue
        provider = _safe_text(raw_provider, redaction)
        status = _safe_text(raw_state.get("status"), redaction)
        if provider is not None and status is not None:
            summaries.append(RunIntegrationSummary(provider=provider, status=status))
    return tuple(summaries)


def _safe_text(value: Any, redaction: RedactionPolicy) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = " ".join(redaction.redact_text(value).split())
    if not rendered:
        return None
    if len(rendered) <= MAX_RUN_LABEL_LENGTH:
        return rendered
    return f"{rendered[: MAX_RUN_LABEL_LENGTH - 1]}…"


def _source_label(value: Any, redaction: RedactionPolicy) -> str:
    return _safe_text(value, redaction) or "No source file"


def _metadata_label(value: Any, redaction: RedactionPolicy) -> str:
    if type(value) is int:
        value = str(value)
    return _safe_text(value, redaction) or "Not provided"


def _started_label(started_at_ms: int, candidate: _ReportCandidate) -> str:
    timestamp = (
        started_at_ms / MILLISECONDS_PER_SECOND
        if started_at_ms
        else candidate.modified_ns / 1_000_000_000
    )
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (OSError, OverflowError, ValueError):
        return "Time unavailable"


def _duration_label(duration_ms: int) -> str:
    if duration_ms < MILLISECONDS_PER_SECOND:
        return f"{duration_ms} ms"
    return f"{duration_ms / MILLISECONDS_PER_SECOND:.2f} s"


def _matches(item: RunCatalogItem, status: str, query: str) -> bool:
    if status not in ("all", item.status):
        return False
    if not query:
        return True
    searchable = (
        item.scenario,
        item.source,
        *item.tags,
        item.failure.activity if item.failure is not None else "",
    )
    return any(query in value.casefold() for value in searchable)


def _catalog_notice(
    *,
    start: int,
    visible_count: int,
    total_count: int,
    invalid_count: int,
    limited: bool,
) -> str:
    messages: list[str] = []
    if total_count:
        suffix = "+" if limited else ""
        messages.append(
            f"Showing {start + 1}-{start + visible_count} of {total_count}{suffix} runs."
        )
    if limited:
        messages.append(
            "Run history scanning reached its local display budget; results and counts are partial."
        )
    if invalid_count:
        messages.append(
            f"{invalid_count} invalid or ambiguous "
            f"{'result was' if invalid_count == 1 else 'results were'} excluded."
        )
    return " ".join(messages)


__all__ = [
    "DEFAULT_RUN_PAGE_SIZE",
    "RunCatalogError",
    "RunCatalogItem",
    "RunCatalogPage",
    "RunDetail",
    "RunFailureSummary",
    "RunIntegrationSummary",
    "VerifiedRunDocument",
    "is_valid_report_run_id",
    "load_run_catalog",
    "load_run_detail",
    "load_verified_run_document",
]
