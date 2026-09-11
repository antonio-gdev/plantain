"""Browser-safe Reflex state for the local workspace overview."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, ParamSpec, TypedDict, cast

import reflex as rx
from pydantic import ValidationError

from plantain.config import Settings
from plantain.dashboard.agent.api_models import (
    ApiContractInspection,
    ApiOperationCandidate,
)
from plantain.dashboard.agent.connection import (
    AgentConfigurationError,
    AgentConnection,
    AgentProvider,
)
from plantain.dashboard.agent.models import (
    MAX_SCENARIO_DIRECTORY_HINT_LENGTH,
    AgentCapability,
    AgentResponseMetadata,
    DashboardCreationResult,
    IntentRouteResult,
    IntentRoutingContext,
    ScenarioAuthorResult,
    ScenarioOperationMatch,
    ScenarioOperationResult,
    UserIntent,
)
from plantain.dashboard.agent.profile import (
    DashboardAgentProfile,
    DashboardAgentProfileError,
    clear_dashboard_agent_profile,
    configure_dashboard_agent_profile,
    load_dashboard_agent_profile,
)
from plantain.dashboard.agent.runtime import (
    DashboardAgentError,
    author_dashboard_api_operation,
    check_dashboard_agent,
    prepare_dashboard_test,
)
from plantain.dashboard.agent_usage import (
    AgentUsageBreakdown,
    AgentUsageTrendPoint,
)
from plantain.dashboard.api_inspection_store import discard_api_inspection
from plantain.dashboard.context_sources import (
    ContextCatalog,
    ContextSourceError,
    ContextSourceKind,
    ContextSourceSummary,
    add_context_source,
    load_context_catalog,
    remove_context_source,
)
from plantain.dashboard.database_workflow_store import (
    DashboardDatabaseWorkflowError,
    bind_database_workflow,
    discard_database_workflow,
    rebind_database_workflow,
)
from plantain.dashboard.draft_store import (
    DashboardDraftError,
    ScenarioDraftPage,
    discard_scenario_draft,
    load_scenario_draft_page,
    store_scenario_draft,
)
from plantain.dashboard.plan_persistence import (
    DashboardPlanSaveError,
    SavedDecisionPlan,
    save_decision_plan,
)
from plantain.dashboard.plan_store import (
    DashboardPlanError,
    DecisionPlanPage,
    PlanDimensionView,
    PlanSourceView,
    PlanTestDetail,
    PlanTestView,
    discard_decision_plan,
    load_decision_plan_case,
    load_decision_plan_page,
    store_decision_plan,
)
from plantain.dashboard.reporting_profile import (
    DashboardReportingProfile,
    DashboardReportingProfileError,
    clear_dashboard_reporting_profile,
    configure_dashboard_reporting_profile,
    load_dashboard_reporting_profile,
)
from plantain.dashboard.runtime_contract import PROJECT_ROOT_ENV
from plantain.dashboard.scenario_paths import default_scenario_directory
from plantain.dashboard.scenario_persistence import (
    DashboardScenarioSaveError,
    SavedScenario,
    save_scenario_draft,
)
from plantain.dashboard.ui_workflow_store import (
    DashboardUiWorkflowError,
    bind_ui_workflow,
    discard_ui_workflow,
    rebind_ui_workflow,
)
from plantain.dashboard.workspace import (
    DashboardWorkspaceError,
    WorkspaceOverview,
    load_workspace_overview,
)
from plantain.errors import ConfigurationError

_CAPABILITY_LABELS: Mapping[AgentCapability, str] = {
    AgentCapability.UI_DISCOVERY: "UI discovery",
    AgentCapability.CRITICAL_DECISION_SPACE: "Critical decision space",
    AgentCapability.API_CONTRACT: "API contract",
    AgentCapability.DATABASE_DISCOVERY: "Database discovery",
    AgentCapability.AUTOMATION_GENERATION: "Automation generation",
    AgentCapability.SCENARIO_EXECUTION: "Scenario execution",
    AgentCapability.WORKSPACE_QUESTION: "Workspace guidance",
}
_CONTEXT_FORM_KINDS = {
    "Application project": ContextSourceKind.APPLICATION,
    "Requirements": ContextSourceKind.REQUIREMENTS,
    "API contract": ContextSourceKind.API_CONTRACT,
}
_CONTEXT_KIND_LABELS = {
    ContextSourceKind.APPLICATION: "Application project",
    ContextSourceKind.REQUIREMENTS: "Requirements",
    ContextSourceKind.API_CONTRACT: "API contract",
}
_LOCAL_AGENT_PROVIDERS = frozenset({AgentProvider.OLLAMA, AgentProvider.LM_STUDIO})
_AGENT_SETUP_PROVIDERS = frozenset(provider.value for provider in AgentProvider)
CONTEXT_SOURCE_PAGE_SIZE = 6
_MILLISECONDS_PER_SECOND = 1_000
_P = ParamSpec("_P")
_WorkflowBindings = tuple[str, str, str, str, str]


class IntentFormData(TypedDict):
    """Statically validated fields submitted by the intent composer."""

    intent: str


class AgentSetupFormData(TypedDict, total=False):
    """Ephemeral provider form fields; credentials are never assigned to state."""

    provider: str
    model: str
    base_url: str
    credential: str


class ResultsSetupFormData(TypedDict, total=False):
    """Result form fields; the Zephyr PAT is never assigned to state."""

    zephyr_base_url: str
    credential: str


class ContextSourceFormData(TypedDict):
    """Typed fields submitted by the local context form."""

    context_kind: str
    context_path: str


class PlanSourceRow(TypedDict):
    """Browser-safe plan evidence provenance."""

    evidence_id: str
    kind: str
    kind_label: str
    label: str
    reference: str
    truncated: bool


class PlanDimensionRow(TypedDict):
    """Browser-safe plan coverage summary."""

    kind: str
    label: str
    summary: str
    evidence_ids: tuple[str, ...]


class PlanTestRow(TypedDict):
    """Browser-safe paginated plan test summary."""

    case_id: str
    title: str
    objective: str
    priority: str
    dimensions: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    summary_limited: bool


class ApiOperationRow(TypedDict):
    """Browser-safe verified API operation preview."""

    operation_key: str
    operation_id: str
    method: str
    path: str
    summary: str
    display_limited: bool


class ScenarioOperationRow(TypedDict):
    """Browser-safe existing-scenario operation preview."""

    scenario_id: str
    name: str
    source: str
    status: str
    status_label: str
    steps: str
    domains: str
    tags: str
    issue: str


class AgentUsageBreakdownRow(TypedDict):
    """Browser-safe provider and model usage row."""

    provider: str
    model: str
    calls: str
    metered: str
    tokens: str


class AgentUsageTrendRow(TypedDict):
    """Browser-safe daily usage point."""

    label: str
    calls: str
    tokens: str
    activity_percent: int
    activity_width: str


def background_event(
    handler: Callable[_P, Coroutine[Any, Any, None]],
) -> Callable[_P, Coroutine[Any, Any, None]]:
    """Preserve handler types across Reflex's dynamically exported event factory."""

    factory = cast("Callable[..., Any]", rx.event)
    decorator = cast(
        "Callable[[Callable[_P, Coroutine[Any, Any, None]]], "
        "Callable[_P, Coroutine[Any, Any, None]]]",
        factory(background=True),
    )
    return decorator(handler)


def dashboard_project_root() -> Path:
    """Return the configured non-secret project location."""

    configured = os.environ.get(PROJECT_ROOT_ENV)
    return Path(configured) if configured else Path.cwd()


class DashboardState(rx.State):
    """Bounded, sanitized state exposed to the local browser."""

    navigation_collapsed: bool = False
    is_loading: bool = False
    scenario_count: int = 0
    run_count: int = 0
    run_count_limited: bool = False
    evidence_count: int = 0
    evidence_count_limited: bool = False
    recent_runs: tuple[dict[str, str], ...] = ()
    quality_available: bool = False
    quality_analyzed_run_count: int = 0
    quality_pass_rate_percent: int = 0
    quality_rate_width: str = "0%"
    quality_passed_count: int = 0
    quality_failed_count: int = 0
    quality_cancelled_count: int = 0
    quality_average_duration: str = "—"
    quality_mixed_outcome_count: int = 0
    usage_history_available: bool = False
    usage_call_count: int = 0
    usage_metered_call_count: int = 0
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_total_tokens: int = 0
    usage_cost_boundary: str = "No recorded usage"
    usage_breakdowns: tuple[AgentUsageBreakdownRow, ...] = ()
    usage_trend: tuple[AgentUsageTrendRow, ...] = ()
    usage_history_limited: bool = False
    usage_history_notice: str = ""
    notice: str = ""
    error_message: str = ""
    agent_ready: bool = False
    agent_connection_state: str = "setup_required"
    agent_provider_name: str = ""
    agent_model: str = ""
    agent_setup_provider: str = AgentProvider.OPENAI.value
    agent_base_url: str = ""
    agent_credential_source: str = "Missing"
    agent_session_configured: bool = False
    agent_profile_notice: str = ""
    agent_status_message: str = "Check Settings to connect an agent."
    agent_busy: bool = False
    agent_check_state: str = "idle"
    agent_error_message: str = ""
    results_allure_enabled: bool = False
    results_zephyr_enabled: bool = False
    results_zephyr_base_url: str = ""
    results_zephyr_attach_report: bool = False
    results_attachment_approved: bool = False
    results_credential_source: str = "Not required"
    results_session_configured: bool = False
    results_busy: bool = False
    results_notice: str = ""
    results_error_message: str = ""
    intent_action: str = ""
    intent_capability: str = ""
    intent_summary: str = ""
    intent_question: str = ""
    intent_plan_steps: tuple[str, ...] = ()
    intent_required_inputs: tuple[str, ...] = ()
    agent_usage_available: bool = False
    agent_input_tokens: int = 0
    agent_output_tokens: int = 0
    agent_total_tokens: int = 0
    api_inspection_id: str = ""
    api_schema_version: str = ""
    api_source_url: str = ""
    api_base_url: str = ""
    api_operation_query: str = ""
    api_operations: tuple[ApiOperationRow, ...] = ()
    api_operation_match_count: int = 0
    api_operation_visible_count: int = 0
    api_operation_matches_limited: bool = False
    draft_id: str = ""
    draft_scenario: str = ""
    draft_step_count: int = 0
    draft_activities: str = ""
    draft_repaired: bool = False
    draft_content: str = ""
    draft_page: int = 1
    draft_page_count: int = 1
    draft_has_previous: bool = False
    draft_has_next: bool = False
    draft_directory: str = ""
    draft_saving: bool = False
    plan_id: str = ""
    plan_feature: str = ""
    plan_repaired: bool = False
    plan_assumptions: tuple[str, ...] = ()
    plan_gaps: tuple[str, ...] = ()
    plan_assumption_count: int = 0
    plan_gap_count: int = 0
    plan_sources: tuple[PlanSourceRow, ...] = ()
    plan_dimensions: tuple[PlanDimensionRow, ...] = ()
    plan_tests: tuple[PlanTestRow, ...] = ()
    plan_source_count: int = 0
    plan_dimension_count: int = 0
    plan_test_count: int = 0
    plan_page: int = 1
    plan_page_count: int = 1
    plan_has_previous: bool = False
    plan_has_next: bool = False
    plan_saving: bool = False
    plan_case_id: str = ""
    plan_case_title: str = ""
    plan_case_objective: str = ""
    plan_case_priority: str = ""
    plan_case_dimensions: tuple[str, ...] = ()
    plan_case_preconditions: tuple[str, ...] = ()
    plan_case_actions: tuple[str, ...] = ()
    plan_case_expected_results: tuple[str, ...] = ()
    plan_case_evidence_ids: tuple[str, ...] = ()
    scenario_operation: str = ""
    scenario_operation_label: str = ""
    scenario_query: str = ""
    scenario_matches: tuple[ScenarioOperationRow, ...] = ()
    scenario_match_count: int = 0
    scenario_match_visible_count: int = 0
    scenario_matches_limited: bool = False
    saved_scenario_id: str = ""
    saved_scenario_name: str = ""
    saved_scenario_path: str = ""
    database_workflow_id: str = ""
    database_workflow_scenario_id: str = ""
    ui_workflow_id: str = ""
    ui_workflow_scenario_id: str = ""
    saved_plan_feature: str = ""
    saved_plan_path: str = ""
    saved_plan_test_count: int = 0
    saved_plan_source_count: int = 0
    context_sources: tuple[dict[str, str], ...] = ()
    context_source_count: int = 0
    context_page: int = 1
    context_total_pages: int = 1
    context_notice: str = ""
    context_error_message: str = ""
    context_busy: bool = False
    agent_context_destination: str = (
        "No provider receives attached context until agent setup is complete."
    )

    @rx.event
    def toggle_navigation(self) -> None:
        """Toggle the desktop navigation between its full and compact forms."""

        self.navigation_collapsed = not self.navigation_collapsed

    @rx.event
    async def refresh(self) -> None:
        """Refresh the bounded workspace projection without blocking the event loop."""

        self.is_loading = True
        self.error_message = ""
        self.context_error_message = ""
        self._apply_agent_connection()
        self._apply_reporting_profile()
        try:
            overview = await asyncio.to_thread(
                load_workspace_overview,
                dashboard_project_root(),
            )
        except DashboardWorkspaceError:
            self._clear_workspace_summary()
            self._clear_workspace_usage()
            self.notice = ""
            self.error_message = "Plantain could not safely read this workspace."
        else:
            self._apply_workspace_summary(overview)
            self._apply_workspace_usage(overview)
            self.notice = overview.notice
        finally:
            self.is_loading = False
        try:
            catalog = await asyncio.to_thread(
                load_context_catalog,
                dashboard_project_root(),
            )
        except ContextSourceError:
            self._clear_context_catalog()
            self.context_error_message = "Plantain could not safely read attached context sources."
        else:
            self._apply_context_catalog(catalog)

    def _clear_workspace_summary(self) -> None:
        self.scenario_count = 0
        self.run_count = 0
        self.run_count_limited = False
        self.evidence_count = 0
        self.evidence_count_limited = False
        self.recent_runs = ()
        self.quality_available = False
        self.quality_analyzed_run_count = 0
        self.quality_pass_rate_percent = 0
        self.quality_rate_width = "0%"
        self.quality_passed_count = 0
        self.quality_failed_count = 0
        self.quality_cancelled_count = 0
        self.quality_average_duration = "—"
        self.quality_mixed_outcome_count = 0

    def _clear_workspace_usage(self) -> None:
        self.usage_history_available = False
        self.usage_call_count = 0
        self.usage_metered_call_count = 0
        self.usage_input_tokens = 0
        self.usage_output_tokens = 0
        self.usage_total_tokens = 0
        self.usage_cost_boundary = "No recorded usage"
        self.usage_breakdowns = ()
        self.usage_trend = ()
        self.usage_history_limited = False
        self.usage_history_notice = ""

    def _apply_workspace_summary(self, overview: WorkspaceOverview) -> None:
        analytics = overview.run_analytics
        self.scenario_count = overview.scenario_count
        self.run_count = overview.run_count
        self.run_count_limited = overview.run_count_limited
        self.evidence_count = overview.evidence_count
        self.evidence_count_limited = overview.evidence_count_limited
        self.quality_available = analytics.analyzed_run_count > 0
        self.quality_analyzed_run_count = analytics.analyzed_run_count
        self.quality_pass_rate_percent = analytics.pass_rate_percent
        self.quality_rate_width = f"{analytics.pass_rate_percent}%"
        self.quality_passed_count = analytics.passed_count
        self.quality_failed_count = analytics.failed_count
        self.quality_cancelled_count = analytics.cancelled_count
        self.quality_average_duration = _workspace_duration_label(analytics.average_duration_ms)
        self.quality_mixed_outcome_count = analytics.mixed_outcome_scenario_count
        self.recent_runs = _recent_run_rows(overview)

    def _apply_workspace_usage(self, overview: WorkspaceOverview) -> None:
        usage = overview.agent_usage
        self.usage_history_available = usage.total_calls > 0
        self.usage_call_count = usage.total_calls
        self.usage_metered_call_count = usage.metered_calls
        self.usage_input_tokens = usage.input_tokens
        self.usage_output_tokens = usage.output_tokens
        self.usage_total_tokens = usage.total_tokens
        self.usage_cost_boundary = usage.cost_boundary
        self.usage_breakdowns = tuple(_usage_breakdown_row(item) for item in usage.breakdowns)
        self.usage_trend = _usage_trend_rows(usage.trend)
        self.usage_history_limited = usage.limited
        self.usage_history_notice = usage.notice

    @rx.event
    def refresh_agent(self) -> None:
        """Refresh non-secret provider readiness from the process environment."""

        self.agent_error_message = ""
        self.agent_check_state = "idle"
        self._apply_agent_connection()

    @rx.event
    def select_agent_provider(self, provider: str) -> None:
        """Project one safe provider choice into the setup form."""

        if provider in _AGENT_SETUP_PROVIDERS:
            self.agent_setup_provider = provider
            self.agent_profile_notice = ""

    @rx.event
    def configure_agent_profile(self, form_data: AgentSetupFormData) -> None:
        """Accept an ephemeral key into the backend-only process profile."""

        if self.agent_busy:
            return
        self.agent_busy = True
        self.agent_error_message = ""
        self.agent_profile_notice = ""
        try:
            profile = configure_dashboard_agent_profile(
                provider=form_data.get("provider", ""),
                model=form_data.get("model", ""),
                base_url=form_data.get("base_url", ""),
                credential=form_data.get("credential", ""),
            )
        except DashboardAgentProfileError:
            self.agent_error_message = (
                "Plantain could not save this profile. Review the required fields."
            )
        else:
            self._set_agent_profile(profile)
            self.agent_check_state = "idle"
            self.agent_profile_notice = (
                "Profile ready for this dashboard process. Test the connection to continue."
            )
        finally:
            self.agent_busy = False

    @rx.event
    def reset_agent_profile(self) -> None:
        """Clear GUI-entered settings and return to environment configuration."""

        if self.agent_busy:
            return
        clear_dashboard_agent_profile()
        self.agent_error_message = ""
        self.agent_check_state = "idle"
        self._apply_agent_connection()
        self.agent_profile_notice = "Session profile cleared; environment settings are active."

    @rx.event
    def set_allure_results(self, enabled: bool) -> None:
        """Stage optional local Allure output for the next profile save."""

        self.results_allure_enabled = enabled

    @rx.event
    def set_zephyr_results(self, enabled: bool) -> None:
        """Stage optional Zephyr publication without changing operator policy."""

        self.results_zephyr_enabled = enabled
        if not enabled:
            self.results_zephyr_attach_report = False
            self.results_attachment_approved = False

    @rx.event
    def set_zephyr_attachment(self, enabled: bool) -> None:
        """Stage sanitized native-result attachment publication."""

        self.results_zephyr_attach_report = enabled
        if not enabled:
            self.results_attachment_approved = False

    @rx.event
    def set_results_attachment_approval(self, approved: bool) -> None:
        """Record explicit attachment data-governance consent."""

        self.results_attachment_approved = approved

    @rx.event
    def configure_results_profile(self, form_data: ResultsSetupFormData) -> None:
        """Validate and activate process-lifetime Results settings."""

        if self.results_busy:
            return
        self.results_busy = True
        self.results_error_message = ""
        self.results_notice = ""
        try:
            profile = self._save_reporting_profile(form_data)
        except (ConfigurationError, DashboardReportingProfileError, OSError):
            self.results_error_message = (
                "Plantain could not save these Results settings. Review the required fields."
            )
        else:
            self._set_reporting_profile(profile)
            self.results_notice = "Results settings are active for this dashboard process."
        finally:
            self.results_busy = False

    @rx.event
    def reset_results_profile(self) -> None:
        """Clear GUI-entered Results settings and restore environment values."""

        if self.results_busy:
            return
        clear_dashboard_reporting_profile()
        self.results_error_message = ""
        self._apply_reporting_profile()
        self.results_notice = "Session settings cleared; environment settings are active."

    @background_event
    async def attach_context(self, form_data: ContextSourceFormData) -> None:
        """Index one explicitly selected source without retaining its path."""

        kind = _CONTEXT_FORM_KINDS.get(form_data.get("context_kind", ""))
        raw_path = form_data.get("context_path", "")
        async with self:
            if self.context_busy:
                return
            if kind is None or not raw_path.strip():
                self.context_error_message = "Choose a source type and local path."
                return
            self.context_busy = True
            self.context_error_message = ""
        try:
            catalog = await asyncio.to_thread(
                add_context_source,
                dashboard_project_root(),
                kind,
                raw_path,
            )
        except ContextSourceError as exc:
            async with self:
                self.context_error_message = str(exc)
        else:
            async with self:
                self._apply_context_catalog(catalog)
        finally:
            async with self:
                self.context_busy = False

    @background_event
    async def detach_context(self, source_id: str) -> None:
        """Forget one attachment without deleting or changing its source."""

        async with self:
            if self.context_busy:
                return
            self.context_busy = True
            self.context_error_message = ""
        try:
            catalog = await asyncio.to_thread(
                remove_context_source,
                dashboard_project_root(),
                source_id,
            )
        except ContextSourceError as exc:
            async with self:
                self.context_error_message = str(exc)
        else:
            async with self:
                self._apply_context_catalog(catalog)
        finally:
            async with self:
                self.context_busy = False

    @background_event
    async def change_context_page(self, offset: int) -> None:
        """Load one adjacent browser-safe context catalog page."""

        if offset not in {-1, 1}:
            return
        async with self:
            target_page = self.context_page + offset
            if self.context_busy or target_page < 1 or target_page > self.context_total_pages:
                return
            self.context_busy = True
            self.context_error_message = ""
        try:
            catalog = await asyncio.to_thread(
                load_context_catalog,
                dashboard_project_root(),
            )
        except ContextSourceError:
            async with self:
                self.context_error_message = (
                    "Plantain could not safely read attached context sources."
                )
        else:
            async with self:
                self._apply_context_catalog(catalog, requested_page=target_page)
        finally:
            async with self:
                self.context_busy = False

    @background_event
    async def check_agent(self) -> None:
        """Run one minimal typed provider check without blocking the dashboard."""

        async with self:
            if self.agent_busy or self.draft_saving:
                return
            self._apply_agent_connection()
            if not self.agent_ready:
                self.agent_error_message = "Complete agent setup before testing the connection."
                self.agent_check_state = "failed"
                return
            self.agent_busy = True
            self.agent_check_state = "checking"
            self.agent_error_message = ""
        try:
            await check_dashboard_agent(dashboard_project_root())
        except DashboardAgentError:
            async with self:
                self.agent_check_state = "failed"
                self.agent_error_message = (
                    "Plantain could not verify this agent connection. "
                    "Review the provider, model, endpoint, and account access."
                )
        else:
            async with self:
                self.agent_check_state = "passed"
                self.agent_status_message = f"{self.agent_provider_name} is initialized and ready."
        finally:
            async with self:
                self.agent_busy = False

    @background_event
    async def route_intent(self, form_data: IntentFormData) -> None:
        """Route one ephemeral request and retain only safe workflow evidence."""

        try:
            source = _continued_intent_source(
                form_data["intent"],
                self.intent_action,
                self.intent_summary,
                self.intent_question,
            )
            intent = UserIntent.model_validate({"prompt": source})
        except ValidationError:
            async with self:
                self.agent_error_message = (
                    "Describe what you want to test in 8,000 characters or fewer."
                )
            return

        async with self:
            if self.agent_busy:
                return
            self._apply_agent_connection()
            if not self.agent_ready:
                self.agent_error_message = "Connect an agent in Settings before creating a test."
                return
            self.agent_busy = True
            self.agent_error_message = ""
            self._clear_saved_scenario()
            self._clear_saved_plan()
            self._clear_intent_result()
            routing_context = IntentRoutingContext(
                canonical_ui_evidence_available=self.evidence_count > 0,
                scenario_available=self.scenario_count > 0,
            )
        try:
            result = await prepare_dashboard_test(
                dashboard_project_root(),
                intent,
                context=routing_context,
            )
            draft_page = _store_creation_draft(result)
            plan_page = _store_creation_plan(result)
        except DashboardAgentError:
            async with self:
                self.agent_error_message = (
                    "Plantain could not safely interpret this request. "
                    "Check the agent connection and try again."
                )
        except DashboardDraftError:
            async with self:
                self.agent_error_message = (
                    "Plantain created a valid draft but could not retain it safely. Try again."
                )
        except DashboardPlanError:
            async with self:
                self.agent_error_message = (
                    "Plantain created a valid plan but could not retain it safely. Try again."
                )
        else:
            async with self:
                self._apply_creation_result(result, draft_page, plan_page)
        finally:
            async with self:
                self.agent_busy = False

    @background_event
    async def select_api_operation(self, operation_key: str) -> None:
        """Create a draft for one operation selected from verified choices."""

        async with self:
            inspection_id = self._begin_api_operation(operation_key)
        if inspection_id is None:
            return
        try:
            result = await author_dashboard_api_operation(
                dashboard_project_root(),
                inspection_id,
                operation_key,
            )
            draft_page = _store_api_authoring_draft(result)
        except (DashboardAgentError, DashboardDraftError):
            async with self:
                self.agent_error_message = (
                    "Plantain could not safely create and retain this API test. "
                    "Choose the operation again or retry the request."
                )
        else:
            async with self:
                self._finish_api_operation(result, draft_page)
        finally:
            async with self:
                self.agent_busy = False

    @rx.event
    def previous_draft_page(self) -> None:
        """Show the previous bounded YAML review page."""

        if not self.draft_saving and self.draft_has_previous:
            self._load_draft_page(self.draft_page - 1)

    @rx.event
    def next_draft_page(self) -> None:
        """Show the next bounded YAML review page."""

        if not self.draft_saving and self.draft_has_next:
            self._load_draft_page(self.draft_page + 1)

    @rx.event
    def previous_plan_page(self) -> None:
        """Show the previous bounded decision-plan page."""

        if not self.plan_saving and self.plan_has_previous:
            self._load_plan_page(self.plan_page - 1)

    @rx.event
    def next_plan_page(self) -> None:
        """Show the next bounded decision-plan page."""

        if not self.plan_saving and self.plan_has_next:
            self._load_plan_page(self.plan_page + 1)

    @rx.event
    def select_plan_case(self, case_id: str) -> None:
        """Load complete detail for one selected plan case."""

        if self.plan_saving or not self.plan_id:
            return
        try:
            detail = load_decision_plan_case(self.plan_id, case_id)
        except DashboardPlanError:
            self.agent_error_message = (
                "This plan case is no longer available. Refresh the plan and try again."
            )
            self._clear_plan_case()
        else:
            self._apply_plan_case(detail)

    @rx.event
    def close_plan_case(self) -> None:
        """Close the selected plan case detail."""

        self._clear_plan_case()

    @rx.event
    def dismiss_draft(self) -> None:
        """Discard the current unsaved draft."""

        if self.draft_saving:
            return
        if self.draft_id:
            discard_scenario_draft(self.draft_id)
        self._clear_draft_state()
        if not self.database_workflow_scenario_id:
            self._discard_database_workflow()
        if not self.ui_workflow_scenario_id:
            self._discard_ui_workflow()

    @rx.event
    def dismiss_plan(self) -> None:
        """Discard the current unsaved decision plan."""

        if self.plan_saving:
            return
        if self.plan_id:
            discard_decision_plan(self.plan_id)
        self._clear_plan_state()

    @rx.event
    def change_draft_directory(self, value: str) -> None:
        """Retain one bounded user-selected folder beneath ``scenarios``."""

        if len(value) > MAX_SCENARIO_DIRECTORY_HINT_LENGTH:
            self.agent_error_message = "The scenario folder is too long."
            return
        self.draft_directory = value

    @rx.event
    def dismiss_saved_scenario(self) -> None:
        """Dismiss the saved-scenario confirmation without changing the file."""

        self._clear_saved_scenario()

    @rx.event
    def dismiss_saved_plan(self) -> None:
        """Dismiss the saved-plan confirmation without changing the file."""

        self._clear_saved_plan()

    @background_event
    async def save_draft(self) -> None:
        """Revalidate and atomically persist the reviewed draft."""

        async with self:
            if not self._begin_draft_save():
                return
            draft_id = self.draft_id
            directory = self.draft_directory
            database_workflow_id = self.database_workflow_id
            database_scenario_id = self.database_workflow_scenario_id
            ui_workflow_id = self.ui_workflow_id
            ui_scenario_id = self.ui_workflow_scenario_id
        try:
            saved = await asyncio.to_thread(
                save_scenario_draft,
                dashboard_project_root(),
                draft_id,
                directory=directory,
            )
        except DashboardScenarioSaveError:
            async with self:
                self.draft_saving = False
                self.agent_error_message = (
                    "Plantain could not save this test. Check the folder and try again."
                )
        else:
            bindings = await asyncio.to_thread(
                _bind_saved_workflows,
                database_workflow_id,
                database_scenario_id,
                ui_workflow_id,
                ui_scenario_id,
                saved,
            )
            async with self:
                self._finish_draft_save(saved, bindings)

    @background_event
    async def save_plan(self) -> None:
        """Revalidate and atomically persist the reviewed decision plan."""

        async with self:
            if not self._begin_plan_save():
                return
            plan_id = self.plan_id
        try:
            saved = await asyncio.to_thread(
                save_decision_plan,
                dashboard_project_root(),
                plan_id,
            )
        except DashboardPlanSaveError:
            async with self:
                self.plan_saving = False
                self.agent_error_message = "Plantain could not save this coverage plan. Try again."
        else:
            async with self:
                self._finish_plan_save(saved)

    def _save_reporting_profile(
        self,
        form_data: ResultsSetupFormData,
    ) -> DashboardReportingProfile:
        settings = Settings.from_env(dashboard_project_root())
        return configure_dashboard_reporting_profile(
            settings,
            allure_enabled=self.results_allure_enabled,
            zephyr_enabled=self.results_zephyr_enabled,
            zephyr_base_url=form_data.get("zephyr_base_url", ""),
            zephyr_attach_report=self.results_zephyr_attach_report,
            attachment_governance_approved=self.results_attachment_approved,
            credential=form_data.get("credential", ""),
        )

    def _apply_reporting_profile(self) -> None:
        try:
            profile = load_dashboard_reporting_profile(Settings.from_env(dashboard_project_root()))
        except (ConfigurationError, OSError):
            self.results_allure_enabled = False
            self.results_zephyr_enabled = False
            self.results_zephyr_base_url = ""
            self.results_zephyr_attach_report = False
            self.results_attachment_approved = False
            self.results_credential_source = "Missing"
            self.results_session_configured = False
            self.results_error_message = "Results configuration is invalid."
            return
        self.results_error_message = ""
        self._set_reporting_profile(profile)

    def _apply_agent_connection(self) -> None:
        try:
            profile = load_dashboard_agent_profile()
        except AgentConfigurationError:
            self.agent_ready = False
            self.agent_connection_state = "setup_required"
            self.agent_provider_name = ""
            self.agent_model = ""
            self.agent_base_url = ""
            self.agent_credential_source = "Missing"
            self.agent_session_configured = False
            self.agent_status_message = "Agent configuration is invalid."
            self.agent_context_destination = (
                "No provider receives attached context until agent setup is valid."
            )
            return
        self._set_agent_profile(profile)

    def _set_agent_profile(self, profile: DashboardAgentProfile) -> None:
        connection = profile.connection
        self._set_agent_connection(connection)
        self.agent_credential_source = _credential_source_label(profile.credential_source)
        self.agent_session_configured = profile.session_configured
        if connection.provider is not None:
            self.agent_setup_provider = connection.provider.value
        self.agent_base_url = (
            connection.base_url if connection.provider is AgentProvider.CUSTOM else ""
        )

    def _set_reporting_profile(self, profile: DashboardReportingProfile) -> None:
        self.results_allure_enabled = profile.allure_enabled
        self.results_zephyr_enabled = profile.zephyr_enabled
        self.results_zephyr_base_url = profile.zephyr_base_url
        self.results_zephyr_attach_report = profile.zephyr_attach_report
        self.results_attachment_approved = profile.attachment_governance_approved
        self.results_credential_source = _credential_source_label(profile.credential_source)
        self.results_session_configured = profile.session_configured

    def _set_agent_connection(self, connection: AgentConnection) -> None:
        self.agent_ready = connection.ready
        self.agent_connection_state = connection.state.value
        self.agent_provider_name = connection.provider_name
        self.agent_model = connection.model
        self.agent_status_message = connection.message
        self.agent_context_destination = _agent_context_destination(connection)

    def _apply_context_catalog(
        self,
        catalog: ContextCatalog,
        *,
        requested_page: int | None = None,
    ) -> None:
        sources, source_count, page, total_pages = _context_catalog_page(
            catalog,
            self.context_page if requested_page is None else requested_page,
        )
        self.context_sources = sources
        self.context_source_count = source_count
        self.context_page = page
        self.context_total_pages = total_pages
        self.context_notice = catalog.notice

    def _clear_context_catalog(self) -> None:
        self.context_sources = ()
        self.context_source_count = 0
        self.context_page = 1
        self.context_total_pages = 1
        self.context_notice = ""

    def _clear_intent_result(self) -> None:
        self._clear_api_inspection()
        self._clear_scenario_operation()
        self.intent_action = ""
        self.intent_capability = ""
        self.intent_summary = ""
        self.intent_question = ""
        self.intent_plan_steps = ()
        self.intent_required_inputs = ()
        self.agent_usage_available = False
        self.agent_input_tokens = 0
        self.agent_output_tokens = 0
        self.agent_total_tokens = 0

    def _apply_intent_result(self, result: IntentRouteResult) -> None:
        decision = result.decision
        self.intent_action = decision.action.value
        self.intent_capability = (
            _CAPABILITY_LABELS[decision.capability] if decision.capability is not None else ""
        )
        self.intent_summary = decision.summary
        self.intent_question = decision.question or ""
        self.intent_plan_steps = tuple(decision.plan_steps)
        self.intent_required_inputs = tuple(decision.required_inputs)
        usage = result.response.usage
        self.agent_usage_available = usage is not None
        if usage is not None:
            self.agent_input_tokens = usage.input_tokens
            self.agent_output_tokens = usage.output_tokens
            self.agent_total_tokens = usage.total_tokens

    def _apply_creation_result(
        self,
        result: DashboardCreationResult,
        draft_page: ScenarioDraftPage | None,
        plan_page: DecisionPlanPage | None,
    ) -> None:
        previous_draft_id = self.draft_id
        previous_plan_id = self.plan_id
        self._clear_draft_state()
        self._clear_plan_state()
        self._clear_api_inspection()
        self._clear_scenario_operation()
        if previous_draft_id:
            discard_scenario_draft(previous_draft_id)
        if previous_plan_id:
            discard_decision_plan(previous_plan_id)
        self._replace_database_workflow(result.database_workflow_id or "")
        self._replace_ui_workflow(result.ui_workflow_id or "")
        self._apply_intent_result(result.route)
        if draft_page is not None:
            self._apply_draft_page(draft_page)
        if plan_page is not None:
            self._apply_plan_page(plan_page)
        if result.api_inspection is not None:
            self._apply_api_inspection(result.api_inspection)
        if result.scenario_operation is not None:
            self._apply_scenario_operation(result.scenario_operation)
        available, input_tokens, output_tokens, total_tokens = _creation_usage(result)
        self.agent_usage_available = available
        self.agent_input_tokens = input_tokens
        self.agent_output_tokens = output_tokens
        self.agent_total_tokens = total_tokens

    def _apply_api_inspection(self, result: ApiContractInspection) -> None:
        self.api_inspection_id = result.inspection_id or ""
        self.api_schema_version = result.schema_version
        self.api_source_url = result.source_url
        self.api_base_url = result.base_url
        self.api_operation_query = result.query
        self.api_operations = tuple(_api_operation_row(item) for item in result.operations)
        self.api_operation_match_count = result.total_matches
        self.api_operation_visible_count = len(result.operations)
        self.api_operation_matches_limited = result.matches_limited

    def _clear_api_inspection(self) -> None:
        if self.api_inspection_id:
            discard_api_inspection(self.api_inspection_id)
        self.api_inspection_id = ""
        self.api_schema_version = ""
        self.api_source_url = ""
        self.api_base_url = ""
        self.api_operation_query = ""
        self.api_operations = ()
        self.api_operation_match_count = 0
        self.api_operation_visible_count = 0
        self.api_operation_matches_limited = False

    def _begin_api_operation(self, operation_key: str) -> str | None:
        if self.agent_busy or not self.api_inspection_id:
            return None
        offered = any(item["operation_key"] == operation_key for item in self.api_operations)
        if not offered:
            self.agent_error_message = (
                "Choose one of the operations returned by the contract inspection."
            )
            return None
        self.agent_busy = True
        self.agent_error_message = ""
        return self.api_inspection_id

    def _finish_api_operation(
        self,
        result: ScenarioAuthorResult,
        page: ScenarioDraftPage,
    ) -> None:
        previous_draft_id = self.draft_id
        self._clear_draft_state()
        if previous_draft_id:
            discard_scenario_draft(previous_draft_id)
        self._apply_draft_page(page)
        self._clear_api_inspection()
        self._merge_authoring_usage(result)

    def _finish_database_continuation(
        self,
        result: ScenarioAuthorResult,
        page: ScenarioDraftPage,
        workflow_id: str,
    ) -> None:
        previous_draft_id = self.draft_id
        self._clear_draft_state()
        if previous_draft_id:
            discard_scenario_draft(previous_draft_id)
        self._apply_draft_page(page)
        self.database_workflow_id = workflow_id
        self._merge_authoring_usage(result)
        self.notice = "The next database discovery step is ready for review."

    def _finish_ui_continuation(
        self,
        result: ScenarioAuthorResult,
        page: ScenarioDraftPage,
        workflow_id: str,
    ) -> None:
        previous_draft_id = self.draft_id
        self._clear_draft_state()
        if previous_draft_id:
            discard_scenario_draft(previous_draft_id)
        self._apply_draft_page(page)
        self.ui_workflow_id = workflow_id
        self._merge_authoring_usage(result)
        self.notice = "The next UI discovery step is ready for review."

    def _merge_authoring_usage(self, result: ScenarioAuthorResult) -> None:
        available, input_tokens, output_tokens, total_tokens = _response_usage(result.responses)
        if not available or not self.agent_usage_available:
            self.agent_usage_available = False
            self.agent_input_tokens = 0
            self.agent_output_tokens = 0
            self.agent_total_tokens = 0
            return
        self.agent_input_tokens += input_tokens
        self.agent_output_tokens += output_tokens
        self.agent_total_tokens += total_tokens

    def _apply_scenario_operation(self, result: ScenarioOperationResult) -> None:
        self.scenario_operation = result.operation.value
        self.scenario_operation_label = _scenario_operation_label(result.operation.value)
        self.scenario_query = result.query
        self.scenario_matches = tuple(_scenario_operation_row(item) for item in result.matches)
        self.scenario_match_count = result.total_matches
        self.scenario_match_visible_count = len(result.matches)
        self.scenario_matches_limited = result.matches_limited

    def _clear_scenario_operation(self) -> None:
        self.scenario_operation = ""
        self.scenario_operation_label = ""
        self.scenario_query = ""
        self.scenario_matches = ()
        self.scenario_match_count = 0
        self.scenario_match_visible_count = 0
        self.scenario_matches_limited = False

    def _apply_draft_page(self, page: ScenarioDraftPage) -> None:
        if page.draft_id != self.draft_id:
            self.draft_directory = _draft_directory(page)
        self.draft_id = page.draft_id
        self.draft_scenario = page.scenario
        self.draft_step_count = page.step_count
        self.draft_activities = ", ".join(page.activities)
        self.draft_repaired = page.repaired
        self.draft_content = page.content
        self.draft_page = page.page
        self.draft_page_count = page.page_count
        self.draft_has_previous = page.has_previous
        self.draft_has_next = page.has_next

    def _apply_plan_page(self, page: DecisionPlanPage) -> None:
        self._clear_plan_case()
        self.plan_id = page.plan_id
        self.plan_feature = page.feature
        self.plan_repaired = page.repaired
        self.plan_assumptions = page.assumptions
        self.plan_gaps = page.gaps
        self.plan_assumption_count = len(page.assumptions)
        self.plan_gap_count = len(page.gaps)
        self.plan_sources = tuple(_plan_source_row(item) for item in page.sources)
        self.plan_dimensions = tuple(_plan_dimension_row(item) for item in page.dimensions)
        self.plan_tests = tuple(_plan_test_row(item) for item in page.tests)
        self.plan_source_count = len(page.sources)
        self.plan_dimension_count = len(page.dimensions)
        self.plan_test_count = page.test_count
        self.plan_page = page.page
        self.plan_page_count = page.page_count
        self.plan_has_previous = page.has_previous
        self.plan_has_next = page.has_next

    def _apply_plan_case(self, detail: PlanTestDetail) -> None:
        self.plan_case_id = detail.case_id
        self.plan_case_title = detail.title
        self.plan_case_objective = detail.objective
        self.plan_case_priority = detail.priority
        self.plan_case_dimensions = tuple(_plan_label(item) for item in detail.dimensions)
        self.plan_case_preconditions = detail.preconditions
        self.plan_case_actions = detail.actions
        self.plan_case_expected_results = detail.expected_results
        self.plan_case_evidence_ids = detail.evidence_ids

    def _begin_plan_save(self) -> bool:
        if self.agent_busy or self.plan_saving or not self.plan_id:
            return False
        self.plan_saving = True
        self.agent_error_message = ""
        return True

    def _begin_draft_save(self) -> bool:
        if self.agent_busy or self.draft_saving or not self.draft_id:
            return False
        self.draft_saving = True
        self.agent_error_message = ""
        return True

    def _finish_draft_save(
        self,
        saved: SavedScenario,
        workflows: _WorkflowBindings,
    ) -> None:
        database_id, database_scenario, ui_id, ui_scenario, warning = workflows
        self._clear_draft_state()
        self.saved_scenario_id = saved.scenario_id
        self.saved_scenario_name = saved.scenario
        self.saved_scenario_path = f"scenarios/{saved.relative_path}"
        self.database_workflow_id = database_id
        self.database_workflow_scenario_id = database_scenario
        self.ui_workflow_id = ui_id
        self.ui_workflow_scenario_id = ui_scenario
        self.scenario_count += 1
        self.notice = f"Saved {saved.scenario} locally."
        self.notice += warning

    def _finish_plan_save(self, saved: SavedDecisionPlan) -> None:
        self._clear_plan_state()
        self.saved_plan_feature = saved.feature
        self.saved_plan_path = f"test-plans/{saved.relative_path}"
        self.saved_plan_test_count = saved.test_count
        self.saved_plan_source_count = saved.source_count
        self.notice = f"Saved the {saved.feature} coverage plan locally."

    def _clear_saved_scenario(self) -> None:
        self.saved_scenario_id = ""
        self.saved_scenario_name = ""
        self.saved_scenario_path = ""

    def _replace_database_workflow(self, workflow_id: str) -> None:
        if self.database_workflow_id and self.database_workflow_id != workflow_id:
            discard_database_workflow(self.database_workflow_id)
        self.database_workflow_id = workflow_id
        self.database_workflow_scenario_id = ""

    def _discard_database_workflow(self) -> None:
        if self.database_workflow_id:
            discard_database_workflow(self.database_workflow_id)
        self.database_workflow_id = ""
        self.database_workflow_scenario_id = ""

    def _replace_ui_workflow(self, workflow_id: str) -> None:
        if self.ui_workflow_id and self.ui_workflow_id != workflow_id:
            discard_ui_workflow(self.ui_workflow_id)
        self.ui_workflow_id = workflow_id
        self.ui_workflow_scenario_id = ""

    def _discard_ui_workflow(self) -> None:
        if self.ui_workflow_id:
            discard_ui_workflow(self.ui_workflow_id)
        self.ui_workflow_id = ""
        self.ui_workflow_scenario_id = ""

    def _clear_saved_plan(self) -> None:
        self.saved_plan_feature = ""
        self.saved_plan_path = ""
        self.saved_plan_test_count = 0
        self.saved_plan_source_count = 0

    def _clear_draft_state(self) -> None:
        self.draft_id = ""
        self.draft_scenario = ""
        self.draft_step_count = 0
        self.draft_activities = ""
        self.draft_repaired = False
        self.draft_content = ""
        self.draft_page = 1
        self.draft_page_count = 1
        self.draft_has_previous = False
        self.draft_has_next = False
        self.draft_directory = ""
        self.draft_saving = False

    def _clear_plan_case(self) -> None:
        self.plan_case_id = ""
        self.plan_case_title = ""
        self.plan_case_objective = ""
        self.plan_case_priority = ""
        self.plan_case_dimensions = ()
        self.plan_case_preconditions = ()
        self.plan_case_actions = ()
        self.plan_case_expected_results = ()
        self.plan_case_evidence_ids = ()

    def _clear_plan_state(self) -> None:
        self.plan_id = ""
        self.plan_feature = ""
        self.plan_repaired = False
        self.plan_assumptions = ()
        self.plan_gaps = ()
        self.plan_assumption_count = 0
        self.plan_gap_count = 0
        self.plan_sources = ()
        self.plan_dimensions = ()
        self.plan_tests = ()
        self.plan_source_count = 0
        self.plan_dimension_count = 0
        self.plan_test_count = 0
        self.plan_page = 1
        self.plan_page_count = 1
        self.plan_has_previous = False
        self.plan_has_next = False
        self.plan_saving = False
        self._clear_plan_case()

    def _load_draft_page(self, requested_page: int) -> None:
        try:
            page = load_scenario_draft_page(self.draft_id, requested_page)
        except DashboardDraftError:
            self.agent_error_message = (
                "This draft is no longer available. Create it again to continue."
            )
            self._clear_draft_state()
        else:
            self._apply_draft_page(page)

    def _load_plan_page(self, requested_page: int) -> None:
        try:
            page = load_decision_plan_page(self.plan_id, requested_page)
        except DashboardPlanError:
            self.agent_error_message = (
                "This coverage plan is no longer available. Create it again to continue."
            )
            self._clear_plan_state()
        else:
            self._apply_plan_page(page)


def _draft_directory(page: ScenarioDraftPage) -> str:
    return page.suggested_directory or default_scenario_directory(page.capability)


def _continued_intent_source(
    answer: str,
    action: str,
    summary: str,
    question: str,
) -> str:
    if action != "clarify":
        return answer
    return (
        "Continue the previously summarized request.\n"
        f"Prior request summary: {summary}\n"
        f"Clarification requested: {question}\n"
        f"User clarification: {answer}"
    )


def _store_creation_draft(
    result: DashboardCreationResult,
) -> ScenarioDraftPage | None:
    if result.authoring is None:
        return None
    capability = result.route.decision.capability
    if capability is None:
        raise DashboardDraftError("The generated scenario workflow is invalid")
    draft_id = store_scenario_draft(result.authoring.draft, capability)
    return load_scenario_draft_page(draft_id)


def _bind_saved_database_workflow(
    workflow_id: str,
    current_scenario_id: str,
    saved: SavedScenario,
) -> str:
    if not workflow_id:
        return ""
    if saved.activities != ("discoverDatabase",):
        discard_database_workflow(workflow_id)
        return ""
    if current_scenario_id:
        rebind_database_workflow(workflow_id, current_scenario_id, saved.scenario_id)
    else:
        bind_database_workflow(workflow_id, saved.scenario_id)
    return saved.scenario_id


def _bind_saved_ui_workflow(
    workflow_id: str,
    current_scenario_id: str,
    saved: SavedScenario,
) -> str:
    if not workflow_id:
        return ""
    if saved.activities != ("capturePageSnapshot",):
        discard_ui_workflow(workflow_id)
        return ""
    if current_scenario_id:
        rebind_ui_workflow(workflow_id, current_scenario_id, saved.scenario_id)
    else:
        bind_ui_workflow(workflow_id, saved.scenario_id)
    return saved.scenario_id


def _bind_saved_workflows(
    database_id: str,
    database_scenario: str,
    ui_id: str,
    ui_scenario: str,
    saved: SavedScenario,
) -> _WorkflowBindings:
    warning = ""
    try:
        database_scenario = _bind_saved_database_workflow(database_id, database_scenario, saved)
    except DashboardDatabaseWorkflowError:
        discard_database_workflow(database_id)
        database_id, database_scenario = "", ""
        warning = " Automatic database continuation is unavailable."
    try:
        ui_scenario = _bind_saved_ui_workflow(ui_id, ui_scenario, saved)
    except DashboardUiWorkflowError:
        discard_ui_workflow(ui_id)
        ui_id, ui_scenario = "", ""
        warning += " Automatic UI continuation is unavailable."
    if not database_scenario:
        database_id = ""
    if not ui_scenario:
        ui_id = ""
    return database_id, database_scenario, ui_id, ui_scenario, warning


def store_database_authoring_draft(
    result: ScenarioAuthorResult,
) -> ScenarioDraftPage:
    """Retain one validated progressive database draft behind an opaque ID."""

    draft_id = store_scenario_draft(
        result.draft,
        AgentCapability.DATABASE_DISCOVERY,
    )
    return load_scenario_draft_page(draft_id)


def store_ui_authoring_draft(
    result: ScenarioAuthorResult,
) -> ScenarioDraftPage:
    """Retain one validated progressive UI draft behind an opaque ID."""

    draft_id = store_scenario_draft(
        result.draft,
        AgentCapability.UI_DISCOVERY,
    )
    return load_scenario_draft_page(draft_id)


def _store_api_authoring_draft(
    result: ScenarioAuthorResult,
) -> ScenarioDraftPage:
    draft_id = store_scenario_draft(
        result.draft,
        AgentCapability.API_CONTRACT,
    )
    return load_scenario_draft_page(draft_id)


def _store_creation_plan(
    result: DashboardCreationResult,
) -> DecisionPlanPage | None:
    if result.planning is None:
        return None
    plan_id = store_decision_plan(result.planning.draft)
    return load_decision_plan_page(plan_id)


def _scenario_operation_row(match: ScenarioOperationMatch) -> ScenarioOperationRow:
    step_label = "step" if match.step_count == 1 else "steps"
    return {
        "scenario_id": match.scenario_id,
        "name": match.name,
        "source": match.source,
        "status": match.status,
        "status_label": _plan_label(match.status),
        "steps": f"{match.step_count} {step_label}",
        "domains": " + ".join(_plan_label(item) for item in match.domains) or "Unclassified",
        "tags": " · ".join(match.tags) or "No tags",
        "issue": match.issue or "",
    }


def _api_operation_row(operation: ApiOperationCandidate) -> ApiOperationRow:
    return {
        "operation_key": operation.operation_key,
        "operation_id": operation.operation_id or "No operation ID",
        "method": operation.method.value,
        "path": operation.path,
        "summary": operation.summary or "No summary supplied",
        "display_limited": operation.display_limited,
    }


def _scenario_operation_label(value: str) -> str:
    labels = {
        "validate": "Validate",
        "run": "Run",
        "rerun": "Run again",
        "diagnose": "Review evidence",
    }
    return labels.get(value, _plan_label(value))


def _plan_source_row(source: PlanSourceView) -> PlanSourceRow:
    return {
        "evidence_id": source.evidence_id,
        "kind": source.kind,
        "kind_label": _plan_label(source.kind),
        "label": source.label,
        "reference": source.reference,
        "truncated": source.truncated,
    }


def _plan_dimension_row(dimension: PlanDimensionView) -> PlanDimensionRow:
    return {
        "kind": dimension.kind,
        "label": _plan_label(dimension.kind),
        "summary": dimension.summary,
        "evidence_ids": dimension.evidence_ids,
    }


def _plan_test_row(test: PlanTestView) -> PlanTestRow:
    return {
        "case_id": test.case_id,
        "title": test.title,
        "objective": test.objective,
        "priority": test.priority,
        "dimensions": tuple(_plan_label(item) for item in test.dimensions),
        "evidence_ids": test.evidence_ids,
        "summary_limited": test.summary_limited,
    }


def _plan_label(value: str) -> str:
    acronyms = {"api": "API", "ui": "UI"}
    return " ".join(acronyms.get(part, part.title()) for part in value.split("_"))


def _workspace_duration_label(value: int | None) -> str:
    if value is None:
        return "—"
    if value < _MILLISECONDS_PER_SECOND:
        return f"{value} ms"
    return f"{value / _MILLISECONDS_PER_SECOND:.2f} s"


def _recent_run_rows(
    overview: WorkspaceOverview,
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "scenario": run.scenario,
            "status": run.status,
            "duration": run.duration,
            "completed": run.completed,
        }
        for run in overview.recent_runs
    )


def _usage_breakdown_row(item: AgentUsageBreakdown) -> AgentUsageBreakdownRow:
    return {
        "provider": item.provider,
        "model": item.model,
        "calls": f"{item.call_count:,}",
        "metered": f"{item.metered_call_count:,} metered",
        "tokens": f"{item.total_tokens:,}",
    }


def _usage_trend_rows(
    points: Sequence[AgentUsageTrendPoint],
) -> tuple[AgentUsageTrendRow, ...]:
    use_tokens = any(point.total_tokens > 0 for point in points)
    values = tuple(point.total_tokens if use_tokens else point.call_count for point in points)
    peak = max(values, default=0)
    rows: list[AgentUsageTrendRow] = []
    for point, value in zip(points, values, strict=True):
        activity_percent = round(value * 100 / peak) if peak else 0
        rows.append(
            {
                "label": point.label,
                "calls": f"{point.call_count:,}",
                "tokens": f"{point.total_tokens:,}",
                "activity_percent": activity_percent,
                "activity_width": f"{activity_percent}%",
            }
        )
    return tuple(rows)


def _creation_usage(
    result: DashboardCreationResult,
) -> tuple[bool, int, int, int]:
    responses = [result.route.response]
    if result.authoring is not None:
        responses.extend(result.authoring.responses)
    if result.planning is not None:
        responses.extend(result.planning.responses)
    return _response_usage(responses)


def _response_usage(
    responses: Sequence[AgentResponseMetadata],
) -> tuple[bool, int, int, int]:
    totals = [
        (usage.input_tokens, usage.output_tokens, usage.total_tokens)
        for response in responses
        if (usage := response.usage) is not None
    ]
    if len(totals) != len(responses):
        return False, 0, 0, 0
    return (
        True,
        sum(item[0] for item in totals),
        sum(item[1] for item in totals),
        sum(item[2] for item in totals),
    )


def _agent_context_destination(connection: AgentConnection) -> str:
    if not connection.ready:
        return "No provider receives attached context until agent setup is complete."
    if connection.provider in _LOCAL_AGENT_PROVIDERS:
        return "Selected context stays on this machine with the configured local provider."
    if connection.provider is not None:
        return (
            "Only selected, redacted excerpts may be sent to "
            f"{connection.provider_name} when you continue."
        )
    return "No provider receives attached context until agent setup is complete."


def _credential_source_label(source: str) -> str:
    return {
        "session": "Session only",
        "environment": "Environment",
        "not_required": "Not required",
        "missing": "Missing",
    }.get(source, "Missing")


def _context_source_row(source: ContextSourceSummary) -> dict[str, str]:
    if not source.available:
        status = "Unavailable"
    elif source.partial:
        status = "Partial"
    else:
        status = "Ready"
    file_label = (
        f"{source.indexed_file_count} "
        f"{'file' if source.indexed_file_count == 1 else 'files'} indexed"
    )
    return {
        "source_id": source.source_id,
        "kind": _CONTEXT_KIND_LABELS[source.kind],
        "label": source.label,
        "files": file_label,
        "status": status,
    }


def _context_catalog_page(
    catalog: ContextCatalog,
    requested_page: int,
) -> tuple[tuple[dict[str, str], ...], int, int, int]:
    source_count = len(catalog.sources)
    total_pages = max(
        1,
        (source_count + CONTEXT_SOURCE_PAGE_SIZE - 1) // CONTEXT_SOURCE_PAGE_SIZE,
    )
    page = min(max(requested_page, 1), total_pages)
    if catalog.focus_source_id is not None:
        focus_index = next(
            (
                index
                for index, source in enumerate(catalog.sources)
                if source.source_id == catalog.focus_source_id
            ),
            None,
        )
        if focus_index is not None:
            page = (focus_index // CONTEXT_SOURCE_PAGE_SIZE) + 1
    start = (page - 1) * CONTEXT_SOURCE_PAGE_SIZE
    visible = catalog.sources[start : start + CONTEXT_SOURCE_PAGE_SIZE]
    return (
        tuple(_context_source_row(source) for source in visible),
        source_count,
        page,
        total_pages,
    )


__all__ = ["DashboardState", "background_event", "dashboard_project_root"]
