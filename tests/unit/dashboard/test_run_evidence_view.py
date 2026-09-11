"""Browser-safe projection coverage for explicitly loaded run evidence."""

from pathlib import Path

import pytest

from plantain.dashboard import run_evidence_view
from plantain.dashboard.run_evidence import (
    EvidencePage,
    RenderedEvidence,
    RunArtifactEvidence,
    RunEvidenceError,
    RunFailureDetail,
    RunFailureEvidence,
    RunOperationEvidence,
    RunStepEvidence,
)

RUN_ID = "a" * 32
EXPECTED_TOTAL = 2


def test_step_page_projects_only_compact_browser_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = EvidencePage(
        items=(
            RunStepEvidence(
                position=1,
                activity="loadApiSchema",
                step_id="schema",
                status="passed",
                duration="20 ms",
            ),
        ),
        total_count=EXPECTED_TOTAL,
        total_count_limited=False,
        page=1,
        page_count=EXPECTED_TOTAL,
        has_previous=False,
        has_next=True,
        notice="Showing 1-1 of 2 steps.",
    )
    monkeypatch.setattr(run_evidence_view, "load_run_steps", lambda *_args, **_kwargs: page)

    view = run_evidence_view.load_run_evidence_view(Path(), RUN_ID, "steps")

    assert view.items == (
        {
            "position": "1",
            "title": "loadApiSchema",
            "subtitle": "schema",
            "status": "passed",
            "status_label": "Passed",
            "duration": "20 ms",
        },
    )
    assert view.total_count == EXPECTED_TOTAL
    assert view.has_next is True
    assert view.failure is None


def test_operation_and_artifact_views_preserve_only_safe_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = RunOperationEvidence(
        position=1,
        domain="api",
        phase="request",
        activity="callSchema",
        step_id="pets",
        operation_type="GET",
        target="https://api.example.test/pets",
        status="failed",
        duration="18 ms",
        error_type="SchemaConformanceError",
        operation_input=RenderedEvidence(text='{"token":"***REDACTED***"}', limited=False),
        operation_expected=RenderedEvidence(text='{"statusCode":200}', limited=False),
        operation_actual=RenderedEvidence(text='{"body":"x"}', limited=True),
    )
    operation_page = EvidencePage(
        items=(operation,),
        total_count=1,
        total_count_limited=False,
        page=1,
        page_count=1,
        has_previous=False,
        has_next=False,
        notice="",
    )
    artifact_page = EvidencePage(
        items=(
            RunArtifactEvidence(
                position=1,
                kind="semantic-snapshot",
                path="snapshots/checkout.semantic.json",
                description="Verified page state",
            ),
        ),
        total_count=1,
        total_count_limited=False,
        page=1,
        page_count=1,
        has_previous=False,
        has_next=False,
        notice="",
    )
    monkeypatch.setattr(
        run_evidence_view,
        "load_run_operations",
        lambda *_args, **_kwargs: operation_page,
    )
    monkeypatch.setattr(
        run_evidence_view,
        "load_run_artifacts",
        lambda *_args, **_kwargs: artifact_page,
    )

    operations = run_evidence_view.load_run_evidence_view(
        Path(),
        RUN_ID,
        "operations",
    )
    artifacts = run_evidence_view.load_run_evidence_view(
        Path(),
        RUN_ID,
        "artifacts",
    )

    assert operations.items[0]["actual_limited"] == "true"
    assert "***REDACTED***" in operations.items[0]["input"]
    assert artifacts.items[0] == {
        "position": "1",
        "title": "semantic-snapshot",
        "path": "snapshots/checkout.semantic.json",
        "description": "Verified page state",
    }


def test_failure_view_is_explicit_bounded_and_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RunFailureEvidence(
        activity="callSchema",
        step_id="pets",
        error_type="SchemaConformanceError",
        message=RenderedEvidence(text="Response did not match", limited=True),
        details=(
            RunFailureDetail(
                label="operation type",
                value=RenderedEvidence(text='"GET"', limited=False),
            ),
        ),
        details_limited=True,
    )
    monkeypatch.setattr(
        run_evidence_view,
        "load_run_failure",
        lambda *_args, **_kwargs: failure,
    )

    view = run_evidence_view.load_run_evidence_view(Path(), RUN_ID, "failure")

    assert view.failure is not None
    assert view.failure.message_limited is True
    assert view.failure.details == (
        {"label": "operation type", "value": '"GET"', "limited": "false"},
    )
    assert view.notice

    monkeypatch.setattr(
        run_evidence_view,
        "load_run_failure",
        lambda *_args, **_kwargs: None,
    )
    empty = run_evidence_view.load_run_evidence_view(Path(), RUN_ID, "failure")
    assert empty.failure is None
    assert empty.total_count == 0


def test_invalid_tab_and_failure_page_fail_closed() -> None:
    with pytest.raises(RunEvidenceError, match="unavailable"):
        run_evidence_view.load_run_evidence_view(Path(), RUN_ID, "raw")
    with pytest.raises(RunEvidenceError, match="only one page"):
        run_evidence_view.load_run_evidence_view(
            Path(),
            RUN_ID,
            "failure",
            page=EXPECTED_TOTAL,
        )
