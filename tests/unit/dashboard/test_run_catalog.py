"""Bounded and browser-safe catalog coverage for immutable native run results."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plantain.dashboard import run_catalog
from plantain.persistence import write_json_atomic
from plantain.security.redaction import REDACTED

FIRST_RUN_ID = "a" * 32
SECOND_RUN_ID = "b" * 32
THIRD_RUN_ID = "c" * 32
DUPLICATE_RUN_ID = "d" * 32
MISMATCHED_RUN_ID = "e" * 32
UNKNOWN_RUN_ID = "f" * 32
OLDER_MODIFIED_NS = 1_000_000_000
NEWER_MODIFIED_NS = 2_000_000_000
STARTED_AT_MS = 1_788_192_000_000
EXPECTED_RUNS = 2
EXPECTED_PAGES = 2
EXPECTED_TAG_OVERFLOW = 1
EXPECTED_INTEGRATIONS = 2
EXPECTED_INVALID_RESULTS = 4


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


def _report(
    run_id: str,
    scenario: str,
    *,
    status: str = "passed",
    tags: list[str] | None = None,
    source_path: str = "ui/checkout.yaml",
) -> dict[str, Any]:
    failed = status == "failed"
    return {
        "scenario": scenario,
        "status": status,
        "duration_ms": 1_250,
        "started_at_ms": STARTED_AT_MS,
        "correlation_id": run_id,
        "source_path": source_path,
        "jira_ticket": "QA-classified",
        "test_case_key": "QA-T42",
        "test_run_key": None,
        "tags": tags or ["smoke"],
        "steps": [
            {
                "activity": "capturePageSnapshot",
                "step_id": "checkout",
                "status": "failed" if failed else "passed",
                "duration_ms": 15,
            }
        ],
        "failure": (
            {
                "activity": "capturePageSnapshot",
                "step_id": "checkout",
                "message": "raw failure content must not reach browser state",
                "exception_type": "AssertionError",
                "details": {"actual": "classified-value"},
            }
            if failed
            else None
        ),
        "artifacts": [{"path": "private/evidence.json"}],
        "operations": [{"operation_actual": "classified-value"}],
        "outputs": {"token": "classified-value"},
        "integrations": {
            "zephyr": {"status": "published"},
            "allure": {"status": "written"},
        },
    }


def _write_report(
    settings: SimpleNamespace,
    report: dict[str, Any],
    *,
    group: str,
    modified_ns: int,
) -> Path:
    run_id = str(report["correlation_id"])
    target = settings.output_dir / "results" / group / f"{run_id}.result.json"
    write_json_atomic(target, report)
    os.utime(target, ns=(modified_ns, modified_ns))
    return target


def test_catalog_filters_pages_redacts_and_loads_metadata_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_report(
        settings,
        _report(
            FIRST_RUN_ID,
            "Inventory",
            source_path="api/inventory.yaml",
        ),
        group="api/inventory",
        modified_ns=OLDER_MODIFIED_NS,
    )
    _write_report(
        settings,
        _report(
            SECOND_RUN_ID,
            "Checkout",
            status="failed",
            tags=["smoke", "classified-context", "api", "nightly"],
        ),
        group="ui/checkout",
        modified_ns=NEWER_MODIFIED_NS,
    )

    first_page = run_catalog.load_run_catalog(tmp_path, page_size=1)

    assert first_page.total_count == EXPECTED_RUNS
    assert first_page.page_count == EXPECTED_PAGES
    assert first_page.has_previous is False
    assert first_page.has_next is True
    assert first_page.total_count_limited is False
    assert first_page.items[0].run_id == SECOND_RUN_ID
    assert first_page.items[0].status == "failed"
    assert first_page.items[0].tags == ("smoke", REDACTED, "api")
    assert first_page.items[0].additional_tag_count == EXPECTED_TAG_OVERFLOW
    assert first_page.items[0].failure is not None
    assert first_page.items[0].failure.error_type == "AssertionError"

    passed = run_catalog.load_run_catalog(tmp_path, status="passed")
    searched = run_catalog.load_run_catalog(tmp_path, query="checkout")
    redacted_search = run_catalog.load_run_catalog(
        tmp_path,
        query="classified-context",
    )

    assert [item.run_id for item in passed.items] == [FIRST_RUN_ID]
    assert [item.run_id for item in searched.items] == [SECOND_RUN_ID]
    assert redacted_search.total_count == 0

    detail = run_catalog.load_run_detail(tmp_path, SECOND_RUN_ID)

    assert detail.summary == first_page.items[0]
    assert detail.jira_ticket == REDACTED
    assert detail.test_case_key == "QA-T42"
    assert detail.test_run_key == "Not provided"
    assert detail.summary.integration_count == EXPECTED_INTEGRATIONS
    assert [item.provider for item in detail.integrations] == ["allure", "zephyr"]
    assert not hasattr(detail, "outputs")
    assert not hasattr(detail, "operations")
    assert not hasattr(detail, "artifacts")


def test_catalog_excludes_mismatched_ambiguous_and_malformed_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    results = settings.output_dir / "results"
    _write_report(
        settings,
        _report(THIRD_RUN_ID, "Retained"),
        group="retained",
        modified_ns=NEWER_MODIFIED_NS,
    )
    write_json_atomic(
        results / "mismatch" / f"{MISMATCHED_RUN_ID}.result.json",
        _report(UNKNOWN_RUN_ID, "Mismatched"),
    )
    write_json_atomic(
        results / "malformed" / "not-a-run.result.json",
        _report(UNKNOWN_RUN_ID, "Malformed"),
    )
    for group in ("duplicate-one", "duplicate-two"):
        write_json_atomic(
            results / group / f"{DUPLICATE_RUN_ID}.result.json",
            _report(DUPLICATE_RUN_ID, "Ambiguous"),
        )

    page = run_catalog.load_run_catalog(tmp_path)

    assert [item.run_id for item in page.items] == [THIRD_RUN_ID]
    assert f"{EXPECTED_INVALID_RESULTS} invalid or ambiguous results were excluded" in page.notice
    with pytest.raises(run_catalog.RunCatalogError, match="no longer available"):
        run_catalog.load_run_detail(tmp_path, DUPLICATE_RUN_ID)


def test_catalog_discloses_read_budget_as_a_lower_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_report(
        settings,
        _report(FIRST_RUN_ID, "Older"),
        group="older",
        modified_ns=OLDER_MODIFIED_NS,
    )
    newest = _write_report(
        settings,
        _report(SECOND_RUN_ID, "Newer"),
        group="newer",
        modified_ns=NEWER_MODIFIED_NS,
    )
    monkeypatch.setattr(
        run_catalog,
        "MAX_RUN_CATALOG_READ_BYTES",
        newest.stat().st_size,
    )

    page = run_catalog.load_run_catalog(tmp_path)

    assert [item.run_id for item in page.items] == [SECOND_RUN_ID]
    assert page.total_count_limited is True
    assert page.notice


@pytest.mark.parametrize(
    ("page", "page_size", "status", "query"),
    [
        (0, run_catalog.DEFAULT_RUN_PAGE_SIZE, "all", ""),
        (True, run_catalog.DEFAULT_RUN_PAGE_SIZE, "all", ""),
        (1, 0, "all", ""),
        (1, run_catalog.MAX_RUN_PAGE_SIZE + 1, "all", ""),
        (1, run_catalog.DEFAULT_RUN_PAGE_SIZE, "unknown", ""),
        (
            1,
            run_catalog.DEFAULT_RUN_PAGE_SIZE,
            "all",
            "x" * (run_catalog.MAX_RUN_SEARCH_LENGTH + 1),
        ),
    ],
)
def test_catalog_rejects_invalid_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    page: int,
    page_size: int,
    status: str,
    query: str,
) -> None:
    _configure(monkeypatch, tmp_path)

    with pytest.raises(run_catalog.RunCatalogError):
        run_catalog.load_run_catalog(
            tmp_path,
            page=page,
            page_size=page_size,
            status=status,
            query=query,
        )


def test_catalog_rejects_symlinked_root_and_invalid_detail_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    settings.output_dir.mkdir()
    (settings.output_dir / "results").symlink_to(outside, target_is_directory=True)

    with pytest.raises(run_catalog.RunCatalogError, match="invalid location"):
        run_catalog.load_run_catalog(tmp_path)
    with pytest.raises(run_catalog.RunCatalogError, match="identifier is invalid"):
        run_catalog.load_run_detail(tmp_path, "../unsafe")


def test_catalog_rejects_excessive_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _configure(monkeypatch, tmp_path)
    _write_report(
        settings,
        _report(
            FIRST_RUN_ID,
            "Too many tags",
            tags=["tag"] * (run_catalog.MAX_RUN_TAGS + 1),
        ),
        group="invalid",
        modified_ns=NEWER_MODIFIED_NS,
    )

    page = run_catalog.load_run_catalog(tmp_path)

    assert page.items == ()
    assert "1 invalid" in page.notice
