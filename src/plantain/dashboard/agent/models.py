"""Strict browser-to-agent contracts for intent-driven test workflows."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from plantain.dashboard.agent.api_models import (
    MAX_API_SCHEMA_HEADERS,
    ApiContractInspection,
    ApiOperationQuery,
    ApiSchemaHeaderReference,
    ApiSchemaReference,
    ApiWorkflowKind,
    validate_schema_headers,
)
from plantain.dashboard.agent.connection import AgentProvider
from plantain.models.common import StrictModel

MAX_USER_INTENT_LENGTH = 8_000
MAX_AGENT_SUMMARY_LENGTH = 600
MAX_CLARIFICATION_LENGTH = 600
MAX_PLAN_STEPS = 8
MAX_REQUIRED_INPUTS = 8
MAX_AGENT_FINISH_REASON_LENGTH = 64
MAX_GENERATED_SCENARIO_CHARACTERS = 2_000_000
MAX_SCENARIO_DRAFT_ACTIVITIES = 8
MAX_SCENARIO_DRAFT_STEPS = 1_000
MAX_SCENARIO_DIRECTORY_HINT_LENGTH = 240
MAX_SCENARIO_OPERATION_QUERY_LENGTH = 240
MAX_DECISION_PLAN_ASSUMPTIONS = 32
MAX_DECISION_PLAN_CITATIONS = 16
MAX_DECISION_PLAN_DIMENSIONS = 6
MAX_DECISION_PLAN_EXPECTATIONS = 16
MAX_DECISION_PLAN_PRECONDITIONS = 16
MAX_DECISION_PLAN_SOURCES = 20
MAX_DECISION_PLAN_STEPS = 24
MAX_DECISION_PLAN_TESTS = 128
MAX_DECISION_PLAN_TEXT_LENGTH = 2_000
BoundedPlanStep = Annotated[str, Field(min_length=1, max_length=240)]
BoundedRequiredInput = Annotated[str, Field(min_length=1, max_length=120)]
BoundedActivityName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$"),
]
BoundedDecisionText = Annotated[
    str,
    Field(min_length=1, max_length=MAX_DECISION_PLAN_TEXT_LENGTH),
]
BoundedEvidenceId = Annotated[
    str,
    Field(
        min_length=27,
        max_length=29,
        pattern=r"^(?:snapshot|source)-[0-9a-f]{20}$",
    ),
]
BoundedPlanCaseId = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9-]*$"),
]
BoundedScenarioId = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
DatabaseWorkflowId = Annotated[
    str,
    Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
]
UiWorkflowId = Annotated[
    str,
    Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
]


class AgentCapability(StrEnum):
    """Internal capability selected from plain-language user intent."""

    UI_DISCOVERY = "ui_discovery"
    CRITICAL_DECISION_SPACE = "critical_decision_space"
    API_CONTRACT = "api_contract"
    DATABASE_DISCOVERY = "database_discovery"
    AUTOMATION_GENERATION = "automation_generation"
    SCENARIO_EXECUTION = "scenario_execution"
    WORKSPACE_QUESTION = "workspace_question"


class IntentAction(StrEnum):
    """Whether the agent can plan or needs one clarification."""

    PLAN = "plan"
    CLARIFY = "clarify"


class ScenarioOperation(StrEnum):
    """Observable action requested for existing local scenarios."""

    VALIDATE = "validate"
    RUN = "run"
    RERUN = "rerun"
    DIAGNOSE = "diagnose"


class ScenarioOperationMatch(StrictModel):
    """One sanitized local scenario match without an executable path."""

    scenario_id: BoundedScenarioId
    name: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=160)
    status: Literal["ready", "needs_review"]
    step_count: int = Field(ge=0, le=MAX_SCENARIO_DRAFT_STEPS)
    domains: list[str] = Field(default_factory=list, max_length=4)
    tags: list[str] = Field(default_factory=list, max_length=3)
    issue: str = Field(default="", max_length=240)


class ScenarioOperationResult(StrictModel):
    """Bounded local matches for one routed existing-scenario operation."""

    operation: ScenarioOperation
    query: str = Field(
        min_length=1,
        max_length=MAX_SCENARIO_OPERATION_QUERY_LENGTH,
    )
    matches: list[ScenarioOperationMatch] = Field(
        default_factory=list,
        max_length=12,
    )
    total_matches: int = Field(ge=0, le=10_000)
    matches_limited: bool = False

    @model_validator(mode="after")
    def truthful_counts(self) -> Self:
        if self.total_matches < len(self.matches):
            raise ValueError("Scenario operation match count is invalid")
        if self.matches_limited != (self.total_matches > len(self.matches)):
            raise ValueError("Scenario operation limit signal is invalid")
        return self


class DecisionEvidenceKind(StrEnum):
    """Evidence categories available to a critical-decision plan."""

    VERIFIED_UI = "verified_ui"
    REQUIREMENTS = "requirements"
    APPLICATION_SOURCE = "application_source"
    API_CONTRACT = "api_contract"
    DATABASE_DISCOVERY = "database_discovery"


class DecisionDimensionKind(StrEnum):
    """Required test-design dimensions without exposing agent internals."""

    CRITICAL_PATH = "critical_path"
    PAIRWISE = "pairwise"
    BOUNDARY = "boundary"
    STATE_TRANSITION = "state_transition"
    FAILURE_RESILIENCE = "failure_resilience"
    ACCESSIBILITY = "accessibility"


class DecisionPriority(StrEnum):
    """User-facing execution priority for a proposed case."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class UserIntent(StrictModel):
    """Bounded natural-language request submitted from the dashboard."""

    prompt: str = Field(min_length=1, max_length=MAX_USER_INTENT_LENGTH)


class IntentDecision(StrictModel):
    """Observable routing result without private model reasoning."""

    action: IntentAction
    capability: AgentCapability | None = None
    summary: str = Field(min_length=1, max_length=MAX_AGENT_SUMMARY_LENGTH)
    question: str | None = Field(default=None, max_length=MAX_CLARIFICATION_LENGTH)
    required_inputs: list[BoundedRequiredInput] = Field(
        default_factory=list,
        max_length=MAX_REQUIRED_INPUTS,
    )
    plan_steps: list[BoundedPlanStep] = Field(
        default_factory=list,
        max_length=MAX_PLAN_STEPS,
    )
    scenario_operation: ScenarioOperation | None = None
    scenario_query: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SCENARIO_OPERATION_QUERY_LENGTH,
    )
    api_workflow: ApiWorkflowKind | None = None
    api_schema_reference: ApiSchemaReference | None = None
    api_schema_headers: list[ApiSchemaHeaderReference] = Field(
        default_factory=list,
        max_length=MAX_API_SCHEMA_HEADERS,
    )
    api_operation_query: ApiOperationQuery | None = None

    @model_validator(mode="after")
    def complete_decision(self) -> Self:
        """Require an actionable plan or one explicit clarification."""

        has_api_details = _has_api_details(self)
        if self.action is IntentAction.CLARIFY:
            _validate_clarification_decision(self, has_api_details)
        else:
            _validate_plan_decision(self, has_api_details)
        return self


def _has_api_details(decision: IntentDecision) -> bool:
    return (
        decision.api_workflow is not None
        or decision.api_schema_reference is not None
        or bool(decision.api_schema_headers)
        or decision.api_operation_query is not None
    )


def _validate_clarification_decision(
    decision: IntentDecision,
    has_api_details: bool,
) -> None:
    if decision.question is None or not decision.question.strip():
        raise ValueError("A clarification decision requires a question")
    if decision.plan_steps:
        raise ValueError("A clarification decision cannot contain plan steps")
    if decision.scenario_operation is not None or decision.scenario_query is not None:
        raise ValueError("A clarification decision cannot select an existing scenario")
    if has_api_details:
        raise ValueError("A clarification decision cannot select an API workflow")


def _validate_plan_decision(
    decision: IntentDecision,
    has_api_details: bool,
) -> None:
    if decision.capability is None:
        raise ValueError("A plan decision requires an internal capability")
    if decision.question is not None:
        raise ValueError("A plan decision cannot contain a clarification question")
    if not decision.plan_steps:
        raise ValueError("A plan decision requires at least one observable step")
    _validate_scenario_operation_decision(decision)
    _validate_api_decision(decision, has_api_details)


def _validate_scenario_operation_decision(decision: IntentDecision) -> None:
    has_scenario_selection = (
        decision.scenario_operation is not None or decision.scenario_query is not None
    )
    if decision.capability is AgentCapability.SCENARIO_EXECUTION:
        if decision.scenario_operation is None or decision.scenario_query is None:
            raise ValueError("Scenario execution requires an operation and local query")
    elif has_scenario_selection:
        raise ValueError("Only scenario execution can select an existing scenario")


def _validate_api_decision(
    decision: IntentDecision,
    has_api_details: bool,
) -> None:
    if decision.capability is not AgentCapability.API_CONTRACT:
        if has_api_details:
            raise ValueError("Only API work can select an API workflow")
        return
    if decision.api_workflow is None:
        raise ValueError("API work requires a request or contract workflow")
    validate_schema_headers(decision.api_schema_headers)
    if decision.api_workflow is ApiWorkflowKind.REQUEST:
        if (
            decision.api_schema_reference is not None
            or decision.api_schema_headers
            or decision.api_operation_query is not None
        ):
            raise ValueError("A generic API request cannot select a schema")
        return
    if decision.api_operation_query is None:
        raise ValueError("API contract work requires an operation query")
    if decision.api_schema_headers and decision.api_schema_reference is None:
        raise ValueError("Schema headers require an explicit schema reference")


class AgentTokenUsage(StrictModel):
    """Exact token counts reported by a provider response."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def total_includes_input_and_output(self) -> Self:
        """Reject internally inconsistent provider usage."""

        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("Total tokens cannot be smaller than input plus output tokens")
        return self


class AgentResponseMetadata(StrictModel):
    """Browser-safe metadata retained from one agent response."""

    provider: AgentProvider
    model: str = Field(min_length=1, max_length=160)
    finish_reason: str = Field(
        min_length=1,
        max_length=MAX_AGENT_FINISH_REASON_LENGTH,
    )
    usage: AgentTokenUsage | None = None


class DecisionEvidenceSource(StrictModel):
    """Browser-safe identity and provenance for one cited evidence excerpt."""

    evidence_id: BoundedEvidenceId
    kind: DecisionEvidenceKind
    label: str = Field(min_length=1, max_length=256)
    reference: str = Field(min_length=1, max_length=2_048)
    truncated: bool = False


class DecisionPlanDimension(StrictModel):
    """One evidence-backed coverage dimension."""

    kind: DecisionDimensionKind
    summary: BoundedDecisionText
    evidence_ids: list[BoundedEvidenceId] = Field(
        default_factory=list,
        max_length=MAX_DECISION_PLAN_CITATIONS,
    )


class DecisionPlanTest(StrictModel):
    """One minimum high-value proposed test with explicit evidence."""

    case_id: BoundedPlanCaseId
    title: str = Field(min_length=1, max_length=256)
    objective: BoundedDecisionText
    priority: DecisionPriority
    dimensions: list[DecisionDimensionKind] = Field(
        min_length=1,
        max_length=MAX_DECISION_PLAN_DIMENSIONS,
    )
    preconditions: list[BoundedDecisionText] = Field(
        default_factory=list,
        max_length=MAX_DECISION_PLAN_PRECONDITIONS,
    )
    actions: list[BoundedDecisionText] = Field(
        min_length=1,
        max_length=MAX_DECISION_PLAN_STEPS,
    )
    expected_results: list[BoundedDecisionText] = Field(
        min_length=1,
        max_length=MAX_DECISION_PLAN_EXPECTATIONS,
    )
    evidence_ids: list[BoundedEvidenceId] = Field(
        min_length=1,
        max_length=MAX_DECISION_PLAN_CITATIONS,
    )


class GeneratedDecisionPlanPayload(StrictModel):
    """Untrusted typed critical-decision payload returned by a provider."""

    feature: str = Field(min_length=1, max_length=256)
    assumptions: list[BoundedDecisionText] = Field(
        default_factory=list,
        max_length=MAX_DECISION_PLAN_ASSUMPTIONS,
    )
    gaps: list[BoundedDecisionText] = Field(
        default_factory=list,
        max_length=MAX_DECISION_PLAN_ASSUMPTIONS,
    )
    dimensions: list[DecisionPlanDimension] = Field(
        min_length=1,
        max_length=MAX_DECISION_PLAN_DIMENSIONS,
    )
    tests: list[DecisionPlanTest] = Field(
        min_length=1,
        max_length=MAX_DECISION_PLAN_TESTS,
    )

    @model_validator(mode="after")
    def unique_and_complete_references(self) -> Self:
        dimension_kinds = [item.kind for item in self.dimensions]
        if len(set(dimension_kinds)) != len(dimension_kinds):
            raise ValueError("Decision-plan dimensions must be unique")
        case_ids = [item.case_id for item in self.tests]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("Decision-plan case identifiers must be unique")
        known_dimensions = frozenset(dimension_kinds)
        if any(
            dimension not in known_dimensions
            for test in self.tests
            for dimension in test.dimensions
        ):
            raise ValueError("Decision-plan tests must reference included dimensions")
        return self


class DecisionPlanDraft(GeneratedDecisionPlanPayload):
    """Validated plan projection that deliberately excludes source content."""

    sources: list[DecisionEvidenceSource] = Field(
        min_length=1,
        max_length=MAX_DECISION_PLAN_SOURCES,
    )
    repaired: bool = False

    @model_validator(mode="after")
    def citations_reference_sources(self) -> Self:
        source_ids = [item.evidence_id for item in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("Decision-plan evidence identifiers must be unique")
        known_sources = frozenset(source_ids)
        citations = (
            evidence_id for dimension in self.dimensions for evidence_id in dimension.evidence_ids
        )
        if any(evidence_id not in known_sources for evidence_id in citations):
            raise ValueError("Decision-plan dimensions cite unknown evidence")
        if any(
            evidence_id not in known_sources
            for test in self.tests
            for evidence_id in test.evidence_ids
        ):
            raise ValueError("Decision-plan tests cite unknown evidence")
        return self


class DecisionPlanAuthorResult(StrictModel):
    """Validated decision plan plus exact bounded provider metadata."""

    draft: DecisionPlanDraft
    responses: list[AgentResponseMetadata] = Field(min_length=1, max_length=2)


class IntentRoutingContext(StrictModel):
    """Non-sensitive workspace readiness signals supplied to the router."""

    canonical_ui_evidence_available: bool = False
    requirements_available: bool = False
    application_source_available: bool = False
    api_schema_available: bool = False
    scenario_available: bool = False
    database_source_available: bool = False


class IntentRouteResult(StrictModel):
    """Browser-safe routing result that deliberately excludes the user prompt."""

    decision: IntentDecision
    response: AgentResponseMetadata


class GeneratedScenarioPayload(StrictModel):
    """Untrusted typed envelope returned by a configured agent provider."""

    scenario_yaml: str = Field(
        min_length=1,
        max_length=MAX_GENERATED_SCENARIO_CHARACTERS,
        repr=False,
    )
    directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SCENARIO_DIRECTORY_HINT_LENGTH,
    )


class ScenarioDraft(StrictModel):
    """Canonical validated scenario prepared for explicit user review."""

    scenario: str = Field(min_length=1, max_length=256)
    yaml_text: str = Field(
        min_length=1,
        max_length=MAX_GENERATED_SCENARIO_CHARACTERS,
        repr=False,
    )
    step_count: int = Field(ge=1, le=MAX_SCENARIO_DRAFT_STEPS)
    activities: list[BoundedActivityName] = Field(
        min_length=1,
        max_length=MAX_SCENARIO_DRAFT_ACTIVITIES,
    )
    suggested_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SCENARIO_DIRECTORY_HINT_LENGTH,
    )
    repaired: bool = False


class ScenarioAuthorResult(StrictModel):
    """Validated draft plus bounded observable provider metadata."""

    draft: ScenarioDraft
    responses: list[AgentResponseMetadata] = Field(min_length=1, max_length=2)


class DashboardCreationResult(StrictModel):
    """One routed request with one optional validated workflow artifact."""

    route: IntentRouteResult
    authoring: ScenarioAuthorResult | None = None
    api_inspection: ApiContractInspection | None = None
    planning: DecisionPlanAuthorResult | None = None
    scenario_operation: ScenarioOperationResult | None = None
    database_workflow_id: DatabaseWorkflowId | None = None
    ui_workflow_id: UiWorkflowId | None = None

    @model_validator(mode="after")
    def at_most_one_artifact(self) -> Self:
        artifact_count = sum(
            artifact is not None
            for artifact in (
                self.authoring,
                self.api_inspection,
                self.planning,
                self.scenario_operation,
            )
        )
        if artifact_count > 1:
            raise ValueError("A dashboard request cannot return multiple workflow artifacts")
        database_authoring = (
            self.authoring is not None
            and self.route.decision.capability is AgentCapability.DATABASE_DISCOVERY
        )
        if (self.database_workflow_id is not None) != database_authoring:
            raise ValueError("A database workflow identifier requires a database authoring draft")
        ui_authoring = (
            self.authoring is not None
            and self.route.decision.capability is AgentCapability.UI_DISCOVERY
        )
        if (self.ui_workflow_id is not None) != ui_authoring:
            raise ValueError("A UI workflow identifier requires a UI authoring draft")
        return self


__all__ = [
    "AgentCapability",
    "AgentResponseMetadata",
    "AgentTokenUsage",
    "DashboardCreationResult",
    "DatabaseWorkflowId",
    "DecisionDimensionKind",
    "DecisionEvidenceKind",
    "DecisionEvidenceSource",
    "DecisionPlanAuthorResult",
    "DecisionPlanDimension",
    "DecisionPlanDraft",
    "DecisionPlanTest",
    "DecisionPriority",
    "GeneratedDecisionPlanPayload",
    "GeneratedScenarioPayload",
    "IntentAction",
    "IntentDecision",
    "IntentRouteResult",
    "IntentRoutingContext",
    "ScenarioAuthorResult",
    "ScenarioDraft",
    "ScenarioOperation",
    "ScenarioOperationMatch",
    "ScenarioOperationResult",
    "UiWorkflowId",
    "UserIntent",
]
