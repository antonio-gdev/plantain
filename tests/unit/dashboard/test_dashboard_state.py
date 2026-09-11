"""Pure browser-state projections for the local dashboard."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from plantain.dashboard import scenario_state
from plantain.dashboard.agent.connection import (
    AgentConnection,
    AgentConnectionState,
    AgentProtocol,
    AgentProvider,
)
from plantain.dashboard.agent_usage import (
    AgentUsageBreakdown,
    AgentUsageTrendPoint,
)
from plantain.dashboard.context_sources import (
    ContextCatalog,
    ContextSourceKind,
    ContextSourceSummary,
)
from plantain.dashboard.reporting_profile import DashboardReportingProfile
from plantain.dashboard.run_progress import DashboardRunProgress
from plantain.dashboard.scenario_catalog import (
    ScenarioCatalogItem,
    ScenarioCatalogSelection,
)
from plantain.dashboard.scenario_execution import DashboardRunOutcome
from plantain.dashboard.scenario_state import (
    MAX_TEST_FILTER_INPUT_LENGTH,
    _activate_run,
    _active_run_row,
    _apply_run_progress,
    _assign_run_job,
    _batch_completion_notice,
    _batch_run_items,
    _catalog_run_name,
    _database_workflow_for_run,
    _merge_test_selection,
    _owns_database_continuation,
    _owns_ui_continuation,
    _run_duration_label,
    _run_outcome_row,
    _run_step_label,
    _run_terminal_row,
    _scenario_catalog_row,
    _selection_validation_notice,
    _test_catalog_filter,
    _test_filter_values,
    _toggle_test_selection,
    _ui_workflow_for_run,
)
from plantain.dashboard.state import (
    CONTEXT_SOURCE_PAGE_SIZE,
    DashboardState,
    _agent_context_destination,
    _context_catalog_page,
    _context_source_row,
    _credential_source_label,
    _usage_breakdown_row,
    _usage_trend_rows,
    _workspace_duration_label,
)
from plantain.dashboard.ui_evidence import UiEvidenceError

EXPECTED_LIVE_PROGRESS_PERCENT = 50

EXPECTED_INDEXED_FILES = 2
EXPECTED_CONTEXT_PAGE_COUNT = 2
EXPECTED_SCENARIO_STEPS = 4
EXPECTED_RUN_DURATION_MS = 1_250
SCENARIO_ID = "a" * 64
SECOND_SCENARIO_ID = "b" * 64
JOB_ID = "c" * 32
WORKFLOW_ID = "d" * 32
SUBSECOND_RUN_DURATION_MS = 999
EXPECTED_USAGE_CALLS = 2
EXPECTED_USAGE_TOKENS = 1_250
EXPECTED_HALF_ACTIVITY = 50
EXPECTED_FULL_ACTIVITY = 100


def _connection(
    *,
    state: AgentConnectionState,
    provider: AgentProvider | None,
    provider_name: str,
) -> AgentConnection:
    return AgentConnection(
        state=state,
        provider=provider,
        provider_name=provider_name,
        protocol=AgentProtocol.OPENAI_COMPATIBLE if provider is not None else None,
        model="example-model" if provider is not None else "",
        base_url="https://provider.example.test" if provider is not None else "",
        credential_environment="PROVIDER_API_KEY" if provider is not None else None,
        available_providers=(),
        message="",
    )


def test_agent_context_destination_discloses_provider_boundary() -> None:
    local = _connection(
        state=AgentConnectionState.READY,
        provider=AgentProvider.OLLAMA,
        provider_name="Ollama",
    )
    hosted = _connection(
        state=AgentConnectionState.READY,
        provider=AgentProvider.OPENAI,
        provider_name="OpenAI",
    )
    incomplete = _connection(
        state=AgentConnectionState.SETUP_REQUIRED,
        provider=AgentProvider.OPENAI,
        provider_name="OpenAI",
    )

    assert "stays on this machine" in _agent_context_destination(local)
    assert "selected, redacted excerpts" in _agent_context_destination(hosted)
    assert "OpenAI" in _agent_context_destination(hosted)
    assert "No provider receives" in _agent_context_destination(incomplete)


def test_agent_credential_source_labels_are_browser_safe() -> None:
    assert _credential_source_label("session") == "Session only"
    assert _credential_source_label("environment") == "Environment"
    assert _credential_source_label("not_required") == "Not required"
    assert _credential_source_label("unexpected") == "Missing"


def test_workspace_duration_labels_are_compact() -> None:
    assert _workspace_duration_label(None) == "—"
    assert _workspace_duration_label(SUBSECOND_RUN_DURATION_MS) == "999 ms"
    assert _workspace_duration_label(EXPECTED_RUN_DURATION_MS) == "1.25 s"


def test_historical_agent_usage_rows_are_browser_safe() -> None:
    row = _usage_breakdown_row(
        AgentUsageBreakdown(
            provider="OpenAI",
            model="example-model",
            call_count=EXPECTED_USAGE_CALLS,
            metered_call_count=1,
            total_tokens=EXPECTED_USAGE_TOKENS,
        )
    )

    assert row == {
        "provider": "OpenAI",
        "model": "example-model",
        "calls": "2",
        "metered": "1 metered",
        "tokens": "1,250",
    }


def test_historical_agent_usage_trend_is_normalized() -> None:
    rows = _usage_trend_rows(
        (
            AgentUsageTrendPoint(label="Sep 08", call_count=1, total_tokens=100),
            AgentUsageTrendPoint(label="Sep 09", call_count=2, total_tokens=50),
        )
    )

    assert rows[0]["activity_percent"] == EXPECTED_FULL_ACTIVITY
    assert rows[0]["activity_width"] == "100%"
    assert rows[1]["activity_percent"] == EXPECTED_HALF_ACTIVITY
    assert rows[1]["activity_width"] == "50%"
    assert rows[1]["calls"] == "2"
    assert rows[1]["tokens"] == "50"


def test_results_profile_projection_is_browser_safe() -> None:
    profile = DashboardReportingProfile(
        allure_enabled=True,
        zephyr_enabled=True,
        zephyr_base_url="https://jira.example.test",
        zephyr_attach_report=True,
        attachment_governance_approved=True,
        credential_source="session",
        session_configured=True,
    )
    state = SimpleNamespace()

    DashboardState._set_reporting_profile(state, profile)

    assert state.results_allure_enabled is True
    assert state.results_zephyr_enabled is True
    assert state.results_zephyr_base_url == "https://jira.example.test"
    assert state.results_zephyr_attach_report is True
    assert state.results_attachment_approved is True
    assert state.results_credential_source == "Session only"
    assert state.results_session_configured is True
    assert not hasattr(state, "credential")


def test_context_source_row_exposes_only_browser_safe_metadata() -> None:
    summary = ContextSourceSummary(
        source_id="a" * 64,
        kind=ContextSourceKind.APPLICATION,
        label="checkout-service",
        indexed_file_count=EXPECTED_INDEXED_FILES,
        partial=True,
        available=True,
    )

    assert _context_source_row(summary) == {
        "source_id": "a" * 64,
        "kind": "Application project",
        "label": "checkout-service",
        "files": "2 files indexed",
        "status": "Partial",
    }


def test_context_catalog_page_bounds_projection_and_honors_new_source_focus() -> None:
    source_count = CONTEXT_SOURCE_PAGE_SIZE + 1
    sources = tuple(
        ContextSourceSummary(
            source_id=f"{index:064x}",
            kind=ContextSourceKind.REQUIREMENTS,
            label=f"source-{index}",
            indexed_file_count=1,
            partial=False,
            available=True,
        )
        for index in range(source_count)
    )
    catalog = ContextCatalog(
        sources=sources,
        notice="",
        focus_source_id=sources[-1].source_id,
    )

    rows, total, page, total_pages = _context_catalog_page(catalog, 1)

    assert total == source_count
    assert page == total_pages == EXPECTED_CONTEXT_PAGE_COUNT
    assert len(rows) == 1
    assert rows[0]["label"] == sources[-1].label


def test_scenario_catalog_row_is_compact_and_browser_safe() -> None:
    item = ScenarioCatalogItem(
        scenario_id="b" * 64,
        name="Checkout",
        source="ui/checkout.yaml",
        status="ready",
        step_count=EXPECTED_SCENARIO_STEPS,
        domains=("UI", "API"),
        tags=("smoke", "checkout"),
        additional_tag_count=1,
        issue="",
    )

    assert _scenario_catalog_row(item) == {
        "scenario_id": "b" * 64,
        "name": "Checkout",
        "source": "ui/checkout.yaml",
        "status": "ready",
        "status_label": "Validated",
        "steps": "4 steps",
        "domains": "UI + API",
        "tags": "smoke · checkout · +1 more",
        "issue": "",
    }


def test_catalog_filters_are_bounded_and_use_engine_tag_semantics() -> None:
    catalog_filter = _test_catalog_filter(
        "  Checkout   flow ",
        "Smoke, API\nsmoke",
        "UI",
        "Slow",
    )

    assert catalog_filter.query == "Checkout flow"
    assert catalog_filter.tag_filter.required_all == frozenset({"smoke", "api"})
    assert catalog_filter.tag_filter.required_any == frozenset({"ui"})
    assert catalog_filter.tag_filter.excluded == frozenset({"slow"})
    assert catalog_filter.active is True
    assert _test_filter_values(" smoke,\napi ,, checkout ") == (
        "smoke",
        "api",
        "checkout",
    )
    with pytest.raises(scenario_state.ScenarioCatalogError, match="too long"):
        _test_catalog_filter("x" * (MAX_TEST_FILTER_INPUT_LENGTH + 1), "", "", "")


def test_catalog_selection_preserves_order_and_toggles_ids() -> None:
    assert _toggle_test_selection((), SCENARIO_ID) == (SCENARIO_ID,)
    assert _toggle_test_selection((SCENARIO_ID,), SCENARIO_ID) == ()
    assert _merge_test_selection(
        (SCENARIO_ID,),
        (SCENARIO_ID, SECOND_SCENARIO_ID),
    ) == (SCENARIO_ID, SECOND_SCENARIO_ID)


def test_selection_validation_reports_counts_and_stale_ids() -> None:
    ready = ScenarioCatalogItem(
        SCENARIO_ID, "Checkout", "ui/checkout.yaml", "ready", 1, ("UI",), (), 0, ""
    )
    review = replace(
        ready,
        scenario_id=SECOND_SCENARIO_ID,
        status="needs_review",
        issue="Review this test.",
    )
    selection = ScenarioCatalogSelection((ready, review), 1, 1)

    assert _selection_validation_notice(selection, ()) == (
        "Validated 2 matching tests: 1 ready, 1 need review."
    )
    assert _selection_validation_notice(selection, (SCENARIO_ID,)) == (
        "Validated 1 selected test: 1 ready, 0 need review."
    )
    assert "selection changed" in _selection_validation_notice(selection, ("f" * 64,)).lower()
    items, issue = _batch_run_items(
        selection,
        (SCENARIO_ID,),
        frozenset(),
    )
    assert items == (ready,)
    assert issue == ""
    assert "need review" in _batch_run_items(selection, (), frozenset())[1]
    assert (
        "already active"
        in _batch_run_items(
            selection,
            (SCENARIO_ID,),
            frozenset({SCENARIO_ID}),
        )[1]
    )
    assert (
        "selection changed"
        in _batch_run_items(
            selection,
            ("f" * 64,),
            frozenset(),
        )[1]
    )
    assert _batch_completion_notice(2, 3, stopped=False) == (
        "Batch complete: 2 of 3 tests finished."
    )
    assert _batch_completion_notice(1, 3, stopped=True) == (
        "Batch stopped after 1 of 3 tests finished."
    )


def test_active_run_projection_preserves_concurrency_and_suppresses_duplicates() -> None:
    active = _activate_run((), SCENARIO_ID)
    active = _activate_run(active, SECOND_SCENARIO_ID)

    assert active == (SCENARIO_ID, SECOND_SCENARIO_ID)
    assert _activate_run(active, SCENARIO_ID) is active

    rows = (_active_run_row(SCENARIO_ID, "Checkout", WORKFLOW_ID),)
    assigned = _assign_run_job(rows, SCENARIO_ID, JOB_ID)

    assert assigned[0]["job_id"] == JOB_ID
    assert assigned[0]["batch_id"] == ""
    assert assigned[0]["name"] == "Checkout"
    assert assigned[0]["database_workflow_id"] == WORKFLOW_ID
    assert assigned[0]["ui_workflow_id"] == ""
    projected = _apply_run_progress(
        assigned,
        SCENARIO_ID,
        DashboardRunProgress(
            correlation_id=JOB_ID,
            status="running",
            phase_label="Step 1 of 2",
            completed_steps=1,
            total_steps=2,
            progress_percent=EXPECTED_LIVE_PROGRESS_PERCENT,
            activity="sendRequest",
            step_id="request",
            latest_operation="Api · Request · GET · Passed",
            timeline=("Scenario execution started.", "Step 1/2 · sendRequest · Running"),
            elapsed_ms=25,
        ),
    )
    assert projected[0]["phase"] == "Step 1 of 2"
    assert projected[0]["progress_percent"] == str(EXPECTED_LIVE_PROGRESS_PERCENT)
    assert projected[0]["progress_width"] == "50%"
    assert projected[0]["progress_label"] == "1 of 2 steps"
    assert projected[0]["activity"] == "sendRequest"
    assert projected[0]["latest_operation"] == "Api · Request · GET · Passed"
    assert projected[0]["elapsed"] == "25 ms"
    assert projected[0]["timeline"].startswith("• Scenario execution started.")
    assert (
        _catalog_run_name(
            ({"scenario_id": SCENARIO_ID, "name": "Checkout"},),
            SCENARIO_ID,
            "Generated checkout",
        )
        == "Checkout"
    )
    assert _catalog_run_name((), SCENARIO_ID, "Generated checkout") == "Generated checkout"
    assert _catalog_run_name((), SCENARIO_ID, "   ") == "Selected test"


def test_run_outcomes_are_compact_and_human_readable() -> None:
    outcome = DashboardRunOutcome(
        scenario_id=SCENARIO_ID,
        name="Checkout",
        status="passed",
        duration_ms=EXPECTED_RUN_DURATION_MS,
        completed_steps=EXPECTED_INDEXED_FILES,
        correlation_id=JOB_ID,
        message="2 steps completed.",
    )

    assert _run_outcome_row(outcome, WORKFLOW_ID) == {
        "scenario_id": SCENARIO_ID,
        "database_workflow_id": WORKFLOW_ID,
        "ui_workflow_id": "",
        "name": "Checkout",
        "status": "passed",
        "status_label": "Passed",
        "message": "2 steps completed.",
        "duration": "1.25 s",
        "steps": "2 steps",
        "correlation_id": JOB_ID,
    }
    assert (
        _run_terminal_row(
            SCENARIO_ID,
            name="Checkout",
            status="cancelled",
            message="Stopped safely.",
        )["status_label"]
        == "Stopped"
    )
    assert (
        _run_terminal_row(
            SCENARIO_ID,
            name="Checkout",
            status="cancelled",
            message="Stopped safely.",
        )["database_workflow_id"]
        == ""
    )
    assert (
        _run_terminal_row(
            SCENARIO_ID,
            name="Checkout",
            status="cancelled",
            message="Stopped safely.",
        )["ui_workflow_id"]
        == ""
    )
    assert _run_duration_label(SUBSECOND_RUN_DURATION_MS) == "999 ms"
    assert _run_step_label(1) == "1 step"


def test_database_continuation_requires_the_exact_passed_run() -> None:
    outcome = DashboardRunOutcome(
        scenario_id=SCENARIO_ID,
        name="Discovery",
        status="passed",
        duration_ms=1,
        completed_steps=1,
        correlation_id=JOB_ID,
        message="1 step completed.",
    )
    rows = (_run_outcome_row(outcome, WORKFLOW_ID),)

    assert _database_workflow_for_run(WORKFLOW_ID, SCENARIO_ID, SCENARIO_ID) == WORKFLOW_ID
    assert _database_workflow_for_run(WORKFLOW_ID, SECOND_SCENARIO_ID, SCENARIO_ID) == ""
    assert _owns_database_continuation(rows, WORKFLOW_ID, SCENARIO_ID, JOB_ID) is True
    assert _owns_database_continuation(rows, WORKFLOW_ID, SCENARIO_ID, "other") is False
    assert _owns_database_continuation(rows, WORKFLOW_ID, SECOND_SCENARIO_ID, JOB_ID) is False


def test_ui_continuation_accepts_only_the_exact_preverified_run() -> None:
    outcome = DashboardRunOutcome(
        scenario_id=SCENARIO_ID,
        name="Discovery",
        status="passed",
        duration_ms=1,
        completed_steps=1,
        correlation_id=JOB_ID,
        message="1 step completed.",
    )
    passed = _run_outcome_row(outcome, "", WORKFLOW_ID)
    failed = _run_outcome_row(
        replace(outcome, status="failed"),
        "",
        WORKFLOW_ID,
    )

    assert _ui_workflow_for_run(WORKFLOW_ID, SCENARIO_ID, SCENARIO_ID) == WORKFLOW_ID
    assert _ui_workflow_for_run(WORKFLOW_ID, SECOND_SCENARIO_ID, SCENARIO_ID) == ""
    assert _owns_ui_continuation((passed,), WORKFLOW_ID, SCENARIO_ID, JOB_ID) is True
    assert _owns_ui_continuation((failed,), WORKFLOW_ID, SCENARIO_ID, JOB_ID) is True
    assert _owns_ui_continuation((passed,), WORKFLOW_ID, SCENARIO_ID, "other") is False


def test_ui_continuation_is_exposed_only_after_evidence_preverification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = DashboardRunOutcome(
        scenario_id=SCENARIO_ID,
        name="Discovery",
        status="failed",
        duration_ms=1,
        completed_steps=0,
        correlation_id=JOB_ID,
        message="The UI action failed.",
    )
    observed: list[tuple[Path, str]] = []
    monkeypatch.setattr(scenario_state, "dashboard_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        scenario_state,
        "load_ui_run_evidence",
        lambda root, run_id: observed.append((root, run_id)),
    )

    retained = asyncio.run(scenario_state._verified_ui_workflow_for_outcome(WORKFLOW_ID, outcome))

    assert retained == WORKFLOW_ID
    assert observed == [(tmp_path, JOB_ID)]

    def reject(_root: Path, _run_id: str) -> None:
        raise UiEvidenceError("synthetic private detail")

    monkeypatch.setattr(scenario_state, "load_ui_run_evidence", reject)
    suppressed = asyncio.run(scenario_state._verified_ui_workflow_for_outcome(WORKFLOW_ID, outcome))

    assert suppressed == ""
