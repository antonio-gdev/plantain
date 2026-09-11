"""Explicit bounded evidence projections for immutable local runs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plantain.dashboard import run_catalog, run_evidence
from plantain.persistence import write_json_atomic
from plantain.security.redaction import REDACTED

RUN_ID = "a" * 32
OTHER_RUN_ID = "b" * 32
EXPECTED_STEPS = 2
EXPECTED_PAGES = 2
ONE_ITEM = 1


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> SimpleNamespace:
    settings = SimpleNamespace(
        output_dir=root / "output",
        sensitive_key_names=("classified",),
    )
    monkeypatch.setattr(
        run_catalog.Settings,
        "from_env",
        lambda _root: settings,
    )
    return settings


def _document(run_id: str = RUN_ID) -> dict[str, Any]:
    return {
        "correlation_id": run_id,
        "steps": [
            {
                "activity": "loadApiSchema",
                "step_id": "schema",
                "status": "passed",
                "duration_ms": 20,
            },
            {
                "activity": "callSchema",
                "step_id": "pets",
                "status": "failed",
                "duration_ms": 1_250,
            },
        ],
        "operations": [
            {
                "domain": "api",
                "phase": "request",
                "activity": "callSchema",
                "step_id": "pets",
                "operation_type": "GET",
                "operation_target": "https://api.example.test/pets",
                "status": "failed",
                "duration_ms": 18,
                "operation_input": {"headers": {"Authorization": "Bearer must-not-appear"}},
                "operation_expected": {"statusCode": 200},
                "operation_actual": {
                    "body": "x" * (run_evidence.MAX_EVIDENCE_VALUE_CHARACTERS + 100)
                },
                "operation_error_type": "SchemaConformanceError",
            },
            {"domain": "malformed"},
        ],
        "artifacts": [
            {
                "kind": "semantic-snapshot",
                "path": "snapshots/checkout.semantic.json",
                "description": "Canonical DOM evidence",
            },
            {
                "kind": "unsafe",
                "path": "../outside.json",
                "description": "Must be excluded",
            },
        ],
        "failure": {
            "activity": "callSchema",
            "step_id": "pets",
            "message": "classified provider detail",
            "exception_type": "SchemaConformanceError",
            "details": {
                "operation_target": "classified endpoint",
                "http_status": 200,
                "password": "must-not-appear",
            },
        },
    }


def _write_document(
    settings: SimpleNamespace,
    value: dict[str, Any],
    *,
    filename_id: str = RUN_ID,
) -> Path:
    target = settings.output_dir / "results/api/pets" / f"{filename_id}.result.json"
    write_json_atomic(target, value)
    return target


def test_step_evidence_is_ordered_and_paginated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_document(settings, _document())

    first = run_evidence.load_run_steps(tmp_path, RUN_ID, page_size=ONE_ITEM)
    second = run_evidence.load_run_steps(
        tmp_path,
        RUN_ID,
        page=EXPECTED_PAGES,
        page_size=ONE_ITEM,
    )

    assert first.total_count == EXPECTED_STEPS
    assert first.page_count == EXPECTED_PAGES
    assert first.has_previous is False
    assert first.has_next is True
    assert first.items[0].position == 1
    assert first.items[0].activity == "loadApiSchema"
    assert first.items[0].duration == "20 ms"
    assert second.items[0].position == EXPECTED_PAGES
    assert second.items[0].status == "failed"
    assert second.items[0].duration == "1.25 s"


def test_operation_evidence_redacts_truncates_and_excludes_malformed_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_document(settings, _document())

    page = run_evidence.load_run_operations(tmp_path, RUN_ID)

    assert page.total_count == ONE_ITEM
    assert "1 malformed operation entry was excluded" in page.notice
    operation = page.items[0]
    assert operation.domain == "api"
    assert operation.target == "https://api.example.test/pets"
    assert operation.operation_input is not None
    assert REDACTED in operation.operation_input.text
    assert "must-not-appear" not in operation.operation_input.text
    assert operation.operation_expected is not None
    assert operation.operation_expected.text == '{"statusCode":200}'
    assert operation.operation_actual is not None
    assert operation.operation_actual.limited is True
    assert operation.operation_actual.text.endswith(run_evidence.TRUNCATION_SUFFIX)


def test_artifact_and_failure_evidence_remain_safe_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_document(settings, _document())

    artifacts = run_evidence.load_run_artifacts(tmp_path, RUN_ID)
    failure = run_evidence.load_run_failure(tmp_path, RUN_ID)

    assert artifacts.total_count == ONE_ITEM
    assert "1 malformed artifact entry was excluded" in artifacts.notice
    assert artifacts.items[0].path == "snapshots/checkout.semantic.json"
    assert failure is not None
    assert failure.message.text == REDACTED
    assert failure.message.limited is False
    assert [item.label for item in failure.details] == [
        "http status",
        "operation target",
    ]
    assert REDACTED in failure.details[1].value.text
    assert all(item.label != "password" for item in failure.details)


def test_evidence_item_limit_is_disclosed_without_blocking_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_document(settings, _document())
    monkeypatch.setattr(run_evidence, "MAX_EVIDENCE_ITEMS", ONE_ITEM)

    page = run_evidence.load_run_steps(tmp_path, RUN_ID)

    assert page.total_count == ONE_ITEM
    assert page.total_count_limited is True
    assert "lower bound" in page.notice


@pytest.mark.parametrize(
    ("page", "page_size"),
    [
        (0, run_evidence.DEFAULT_EVIDENCE_PAGE_SIZE),
        (True, run_evidence.DEFAULT_EVIDENCE_PAGE_SIZE),
        (1, 0),
        (1, run_evidence.MAX_EVIDENCE_PAGE_SIZE + 1),
    ],
)
def test_evidence_rejects_invalid_page_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    page: int,
    page_size: int,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_document(settings, _document())

    with pytest.raises(run_evidence.RunEvidenceError):
        run_evidence.load_run_steps(
            tmp_path,
            RUN_ID,
            page=page,
            page_size=page_size,
        )


def test_evidence_rejects_invalid_collections_and_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    invalid = _document()
    invalid["operations"] = {}
    _write_document(settings, invalid)

    with pytest.raises(run_evidence.RunEvidenceError, match="operations evidence"):
        run_evidence.load_run_operations(tmp_path, RUN_ID)

    mismatched_root = tmp_path / "mismatched"
    mismatched = _configure(monkeypatch, mismatched_root)
    _write_document(
        mismatched,
        _document(OTHER_RUN_ID),
        filename_id=RUN_ID,
    )
    with pytest.raises(run_evidence.RunEvidenceError, match="unavailable"):
        run_evidence.load_run_steps(mismatched_root, RUN_ID)
