"""Browser-safe views of explicitly requested immutable run evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from plantain.dashboard.run_evidence import (
    DEFAULT_EVIDENCE_PAGE_SIZE,
    MAX_EVIDENCE_PAGE_SIZE,
    EvidencePage,
    RenderedEvidence,
    RunArtifactEvidence,
    RunEvidenceError,
    RunFailureEvidence,
    RunOperationEvidence,
    RunStepEvidence,
    load_run_artifacts,
    load_run_failure,
    load_run_operations,
    load_run_steps,
)

DEFAULT_RUN_EVIDENCE_TAB = "steps"
RUN_EVIDENCE_TABS = frozenset({"steps", "operations", "artifacts", "failure"})

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RunFailureView:
    """Browser-safe failure summary and allowlisted details."""

    activity: str
    step_id: str
    error_type: str
    message: str
    message_limited: bool
    details: tuple[dict[str, str], ...]
    details_limited: bool


@dataclass(frozen=True, slots=True)
class RunEvidenceView:
    """One explicitly loaded evidence tab ready for Reflex state."""

    tab: str
    items: tuple[dict[str, str], ...]
    total_count: int
    total_count_limited: bool
    page: int
    page_count: int
    has_previous: bool
    has_next: bool
    notice: str
    failure: RunFailureView | None


def load_run_evidence_view(
    project_root: Path,
    run_id: str,
    tab: str,
    *,
    page: int = 1,
    page_size: int = DEFAULT_EVIDENCE_PAGE_SIZE,
) -> RunEvidenceView:
    """Load one verified evidence tab through a browser-safe projection."""

    if tab not in RUN_EVIDENCE_TABS:
        raise RunEvidenceError("The requested run evidence tab is unavailable")
    if tab == "steps":
        return _page_view(
            tab,
            load_run_steps(project_root, run_id, page=page, page_size=page_size),
            _step_row,
        )
    if tab == "operations":
        return _page_view(
            tab,
            load_run_operations(
                project_root,
                run_id,
                page=page,
                page_size=page_size,
            ),
            _operation_row,
        )
    if tab == "artifacts":
        return _page_view(
            tab,
            load_run_artifacts(
                project_root,
                run_id,
                page=page,
                page_size=page_size,
            ),
            _artifact_row,
        )
    _validate_failure_page(page, page_size)
    return _failure_view(load_run_failure(project_root, run_id))


def _page_view(
    tab: str,
    page: EvidencePage[T],
    projector: Callable[[T], dict[str, str]],
) -> RunEvidenceView:
    return RunEvidenceView(
        tab=tab,
        items=tuple(projector(item) for item in page.items),
        total_count=page.total_count,
        total_count_limited=page.total_count_limited,
        page=page.page,
        page_count=page.page_count,
        has_previous=page.has_previous,
        has_next=page.has_next,
        notice=page.notice,
        failure=None,
    )


def _step_row(item: RunStepEvidence) -> dict[str, str]:
    return {
        "position": str(item.position),
        "title": item.activity,
        "subtitle": item.step_id,
        "status": item.status,
        "status_label": _status_label(item.status),
        "duration": item.duration,
    }


def _operation_row(item: RunOperationEvidence) -> dict[str, str]:
    operation_input, input_limited = _rendered(item.operation_input)
    expected, expected_limited = _rendered(item.operation_expected)
    actual, actual_limited = _rendered(item.operation_actual)
    return {
        "position": str(item.position),
        "title": item.operation_type,
        "subtitle": f"{item.domain} · {item.phase}",
        "activity": item.activity,
        "step_id": item.step_id,
        "target": item.target,
        "status": item.status,
        "status_label": _status_label(item.status),
        "duration": item.duration,
        "error_type": item.error_type,
        "input": operation_input,
        "input_limited": input_limited,
        "expected": expected,
        "expected_limited": expected_limited,
        "actual": actual,
        "actual_limited": actual_limited,
    }


def _artifact_row(item: RunArtifactEvidence) -> dict[str, str]:
    return {
        "position": str(item.position),
        "title": item.kind,
        "path": item.path,
        "description": item.description,
    }


def _failure_view(failure: RunFailureEvidence | None) -> RunEvidenceView:
    if failure is None:
        return RunEvidenceView(
            tab="failure",
            items=(),
            total_count=0,
            total_count_limited=False,
            page=1,
            page_count=0,
            has_previous=False,
            has_next=False,
            notice="No failure evidence was recorded for this run.",
            failure=None,
        )
    details = tuple(
        {
            "label": item.label,
            "value": item.value.text,
            "limited": _boolean_label(item.value.limited),
        }
        for item in failure.details
    )
    projected = RunFailureView(
        activity=failure.activity,
        step_id=failure.step_id,
        error_type=failure.error_type,
        message=failure.message.text,
        message_limited=failure.message.limited,
        details=details,
        details_limited=failure.details_limited,
    )
    notice = (
        "Additional allowlisted failure details are not displayed."
        if failure.details_limited
        else ""
    )
    return RunEvidenceView(
        tab="failure",
        items=(),
        total_count=1,
        total_count_limited=False,
        page=1,
        page_count=1,
        has_previous=False,
        has_next=False,
        notice=notice,
        failure=projected,
    )


def _rendered(value: RenderedEvidence | None) -> tuple[str, str]:
    if value is None:
        return "", "false"
    return value.text, _boolean_label(value.limited)


def _boolean_label(value: bool) -> str:
    return "true" if value else "false"


def _status_label(status: str) -> str:
    labels = {
        "cancelled": "Stopped",
        "failed": "Failed",
        "passed": "Passed",
        "started": "Running",
    }
    return labels.get(status, status.replace("_", " ").title())


def _validate_failure_page(page: int, page_size: int) -> None:
    if not isinstance(page, int) or isinstance(page, bool) or page != 1:
        raise RunEvidenceError("Failure evidence has only one page")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > MAX_EVIDENCE_PAGE_SIZE
    ):
        raise RunEvidenceError(
            f"The evidence page size must be between 1 and {MAX_EVIDENCE_PAGE_SIZE}"
        )


__all__ = [
    "DEFAULT_RUN_EVIDENCE_TAB",
    "RUN_EVIDENCE_TABS",
    "RunEvidenceView",
    "RunFailureView",
    "load_run_evidence_view",
]
