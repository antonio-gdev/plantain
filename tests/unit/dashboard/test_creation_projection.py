"""Pure state projections for dashboard intent-to-draft creation."""

from __future__ import annotations

from dataclasses import replace

from plantain.dashboard.agent.api_models import ApiOperationCandidate, ApiWorkflowKind
from plantain.dashboard.agent.connection import AgentProvider
from plantain.dashboard.agent.models import (
    AgentCapability,
    AgentResponseMetadata,
    AgentTokenUsage,
    DashboardCreationResult,
    DecisionDimensionKind,
    DecisionEvidenceKind,
    DecisionEvidenceSource,
    DecisionPlanAuthorResult,
    DecisionPlanDimension,
    DecisionPlanDraft,
    DecisionPlanTest,
    DecisionPriority,
    IntentAction,
    IntentDecision,
    IntentRouteResult,
    ScenarioAuthorResult,
    ScenarioDraft,
    ScenarioOperation,
    ScenarioOperationMatch,
    ScenarioOperationResult,
)
from plantain.dashboard.draft_store import discard_scenario_draft
from plantain.dashboard.plan_store import discard_decision_plan
from plantain.dashboard.state import (
    _api_operation_row,
    _continued_intent_source,
    _creation_usage,
    _draft_directory,
    _plan_dimension_row,
    _plan_source_row,
    _plan_test_row,
    _scenario_operation_label,
    _scenario_operation_row,
    _store_creation_draft,
    _store_creation_plan,
)
from plantain.models.api import HttpMethod

ROUTE_INPUT_TOKENS = 3
ROUTE_OUTPUT_TOKENS = 2
ROUTE_TOTAL_TOKENS = 5
AUTHOR_INPUT_TOKENS = 7
AUTHOR_OUTPUT_TOKENS = 4
AUTHOR_TOTAL_TOKENS = 11
EXPECTED_INPUT_TOKENS = 10
EXPECTED_OUTPUT_TOKENS = 6
EXPECTED_TOTAL_TOKENS = 16
SNAPSHOT_EVIDENCE_ID = f"snapshot-{'c' * 20}"


def _metadata(usage: AgentTokenUsage | None) -> AgentResponseMetadata:
    return AgentResponseMetadata(
        provider=AgentProvider.OLLAMA,
        model="local-model",
        finish_reason="stop",
        usage=usage,
    )


def _result(*, author_usage: AgentTokenUsage | None) -> DashboardCreationResult:
    route = IntentRouteResult(
        decision=IntentDecision(
            action=IntentAction.PLAN,
            capability=AgentCapability.API_CONTRACT,
            api_workflow=ApiWorkflowKind.REQUEST,
            summary="Create a health endpoint test.",
            plan_steps=["Create and validate a scenario."],
        ),
        response=_metadata(
            AgentTokenUsage(
                input_tokens=ROUTE_INPUT_TOKENS,
                output_tokens=ROUTE_OUTPUT_TOKENS,
                total_tokens=ROUTE_TOTAL_TOKENS,
            )
        ),
    )
    authoring = ScenarioAuthorResult(
        draft=ScenarioDraft(
            scenario="Health",
            yaml_text=(
                "scenario: Health\n"
                "steps:\n"
                "  - sendRequest:\n"
                "      id: health\n"
                "      endpoint: https://api.example.test/health\n"
                "      method: GET\n"
            ),
            step_count=1,
            activities=["sendRequest"],
        ),
        responses=[_metadata(author_usage)],
    )
    return DashboardCreationResult(route=route, authoring=authoring)


def _planning_result(
    *,
    plan_usage: AgentTokenUsage | None,
) -> DashboardCreationResult:
    base = _result(author_usage=None)
    draft = DecisionPlanDraft(
        feature="Checkout",
        sources=[
            DecisionEvidenceSource(
                evidence_id=SNAPSHOT_EVIDENCE_ID,
                kind=DecisionEvidenceKind.VERIFIED_UI,
                label="Checkout",
                reference="checkout.semantic.json",
            )
        ],
        assumptions=["The verified snapshot represents the current application."],
        gaps=["Payment-provider behavior requires additional evidence."],
        dimensions=[
            DecisionPlanDimension(
                kind=DecisionDimensionKind.CRITICAL_PATH,
                summary="Cover the verified checkout path.",
                evidence_ids=[SNAPSHOT_EVIDENCE_ID],
            )
        ],
        tests=[
            DecisionPlanTest(
                case_id="checkout-path",
                title="Checkout path",
                objective="Verify the evidence-backed checkout path.",
                priority=DecisionPriority.CRITICAL,
                dimensions=[DecisionDimensionKind.CRITICAL_PATH],
                preconditions=["An item is available."],
                actions=["Follow the observed flow."],
                expected_results=["Checkout completes."],
                evidence_ids=[SNAPSHOT_EVIDENCE_ID],
            )
        ],
    )
    planning = DecisionPlanAuthorResult(
        draft=draft,
        responses=[_metadata(plan_usage)],
    )
    return DashboardCreationResult(route=base.route, planning=planning)


def test_clarification_continues_from_sanitized_summary() -> None:
    initial = _continued_intent_source("Test checkout", "", "", "")
    continued = _continued_intent_source(
        "Use the staging application.",
        "clarify",
        "Test the checkout flow.",
        "Which application should Plantain inspect?",
    )

    assert initial == "Test checkout"
    assert "Prior request summary: Test the checkout flow." in continued
    assert "Clarification requested: Which application" in continued
    assert "User clarification: Use the staging application." in continued


def test_creation_usage_sums_every_reported_provider_call() -> None:
    result = _result(
        author_usage=AgentTokenUsage(
            input_tokens=AUTHOR_INPUT_TOKENS,
            output_tokens=AUTHOR_OUTPUT_TOKENS,
            total_tokens=AUTHOR_TOTAL_TOKENS,
        )
    )

    assert _creation_usage(result) == (
        True,
        EXPECTED_INPUT_TOKENS,
        EXPECTED_OUTPUT_TOKENS,
        EXPECTED_TOTAL_TOKENS,
    )


def test_creation_usage_is_unavailable_when_any_call_omits_counts() -> None:
    assert _creation_usage(_result(author_usage=None)) == (False, 0, 0, 0)


def test_creation_usage_includes_every_planning_call() -> None:
    result = _planning_result(
        plan_usage=AgentTokenUsage(
            input_tokens=AUTHOR_INPUT_TOKENS,
            output_tokens=AUTHOR_OUTPUT_TOKENS,
            total_tokens=AUTHOR_TOTAL_TOKENS,
        )
    )

    assert _creation_usage(result) == (
        True,
        EXPECTED_INPUT_TOKENS,
        EXPECTED_OUTPUT_TOKENS,
        EXPECTED_TOTAL_TOKENS,
    )


def test_existing_scenario_operation_has_safe_route_only_projection() -> None:
    match = ScenarioOperationMatch(
        scenario_id="a" * 64,
        name="Checkout",
        source="ui/checkout.yaml",
        status="ready",
        step_count=3,
        domains=["api", "ui"],
        tags=["smoke"],
        issue="",
    )
    result = DashboardCreationResult(
        route=_result(author_usage=None).route,
        scenario_operation=ScenarioOperationResult(
            operation=ScenarioOperation.RUN,
            query="checkout",
            matches=[match],
            total_matches=1,
            matches_limited=False,
        ),
    )

    assert _creation_usage(result) == (
        True,
        ROUTE_INPUT_TOKENS,
        ROUTE_OUTPUT_TOKENS,
        ROUTE_TOTAL_TOKENS,
    )
    assert _scenario_operation_label("rerun") == "Run again"
    assert _scenario_operation_row(match) == {
        "scenario_id": "a" * 64,
        "name": "Checkout",
        "source": "ui/checkout.yaml",
        "status": "ready",
        "status_label": "Ready",
        "steps": "3 steps",
        "domains": "API + UI",
        "tags": "smoke",
        "issue": "",
    }


def test_api_operation_projection_contains_only_safe_choice_metadata() -> None:
    operation = ApiOperationCandidate(
        operation_key="d" * 64,
        operation_id="",
        method=HttpMethod.GET,
        path="/pets",
        summary="",
        display_limited=True,
    )

    assert _api_operation_row(operation) == {
        "operation_key": "d" * 64,
        "operation_id": "No operation ID",
        "method": "GET",
        "path": "/pets",
        "summary": "No summary supplied",
        "display_limited": True,
    }


def test_planning_result_enters_store_through_safe_projections() -> None:
    page = _store_creation_plan(_planning_result(plan_usage=None))

    assert page is not None
    assert page.feature == "Checkout"
    assert _plan_source_row(page.sources[0]) == {
        "evidence_id": SNAPSHOT_EVIDENCE_ID,
        "kind": "verified_ui",
        "kind_label": "Verified UI",
        "label": "Checkout",
        "reference": "checkout.semantic.json",
        "truncated": False,
    }
    assert _plan_dimension_row(page.dimensions[0])["evidence_ids"] == (SNAPSHOT_EVIDENCE_ID,)
    assert _plan_test_row(page.tests[0])["case_id"] == "checkout-path"
    assert "content" not in _plan_source_row(page.sources[0])
    assert discard_decision_plan(page.plan_id) is True


def test_creation_result_enters_store_through_opaque_page() -> None:
    page = _store_creation_draft(_result(author_usage=None))

    assert page is not None
    assert page.scenario == "Health"
    assert page.activities == ("sendRequest",)
    assert page.draft_id not in page.content
    assert discard_scenario_draft(page.draft_id) is True


def test_draft_directory_prefers_suggestion_over_capability_default() -> None:
    page = _store_creation_draft(_result(author_usage=None))

    assert page is not None
    assert _draft_directory(page) == "generated/api"
    suggested = replace(
        page,
        suggested_directory="Payments Team / Regression Cases",
    )
    assert _draft_directory(suggested) == "Payments Team / Regression Cases"
    assert discard_scenario_draft(page.draft_id) is True
