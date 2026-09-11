"""Pure browser-state projections for immutable dashboard run history."""

from plantain.dashboard.run_catalog import (
    RunCatalogItem,
    RunDetail,
    RunFailureSummary,
    RunIntegrationSummary,
)
from plantain.dashboard.run_state import (
    _run_catalog_row,
    _run_detail_projection,
    _run_status_label,
)

RUN_ID = "a" * 32
EXPECTED_STEPS = 3
EXPECTED_PASSED_STEPS = 2
EXPECTED_INTEGRATIONS = 2


def _item() -> RunCatalogItem:
    return RunCatalogItem(
        run_id=RUN_ID,
        scenario="Checkout",
        status="failed",
        source="ui/checkout.yaml",
        started="2026-09-04 12:00 UTC",
        duration="1.25 s",
        step_count=EXPECTED_STEPS,
        passed_step_count=EXPECTED_PASSED_STEPS,
        failed_step_count=1,
        tags=("smoke", "checkout"),
        additional_tag_count=1,
        integration_count=EXPECTED_INTEGRATIONS,
        failure=RunFailureSummary(
            activity="capturePageSnapshot",
            step_id="checkout",
            error_type="UiAssertionError",
        ),
    )


def test_run_catalog_row_is_compact_and_browser_safe() -> None:
    row = _run_catalog_row(_item())

    assert row == {
        "run_id": RUN_ID,
        "short_id": "aaaaaaaa",
        "scenario": "Checkout",
        "status": "failed",
        "status_label": "Failed",
        "source": "ui/checkout.yaml",
        "started": "2026-09-04 12:00 UTC",
        "duration": "1.25 s",
        "steps": "2/3 steps passed",
        "tags": "smoke · checkout · +1 more",
        "failure": "capturePageSnapshot · checkout",
        "failure_type": "UiAssertionError",
        "integrations": "2 integrations",
    }
    assert "message" not in row
    assert "operations" not in row
    assert "artifacts" not in row


def test_run_detail_projection_contains_only_metadata_and_integrations() -> None:
    detail = RunDetail(
        summary=_item(),
        jira_ticket="QA-42",
        test_case_key="QA-T42",
        test_run_key="Not provided",
        integrations=(
            RunIntegrationSummary(provider="allure", status="written"),
            RunIntegrationSummary(provider="zephyr", status="published"),
        ),
        additional_integration_count=0,
    )

    summary, integrations = _run_detail_projection(detail)

    assert summary["jira_ticket"] == "QA-42"
    assert summary["test_case_key"] == "QA-T42"
    assert summary["test_run_key"] == "Not provided"
    assert integrations == (
        {"provider": "allure", "status": "written"},
        {"provider": "zephyr", "status": "published"},
    )


def test_run_status_labels_use_human_language() -> None:
    assert _run_status_label("passed") == "Passed"
    assert _run_status_label("failed") == "Failed"
    assert _run_status_label("cancelled") == "Stopped"
    assert _run_status_label("unexpected") == "Unavailable"
