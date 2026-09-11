"""Dashboard-owned lifecycle for safe provider routing and connection checks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from plantain.config import Settings
from plantain.dashboard.agent.api_grounding import (
    ApiGroundingError,
    ground_cached_api_operation,
    inspect_api_contract,
)
from plantain.dashboard.agent.api_models import (
    ApiContractEvidence,
    ApiContractInspection,
    ApiWorkflowKind,
)
from plantain.dashboard.agent.authoring import (
    ScenarioAuthor,
    ScenarioAuthoringError,
    supports_scenario_authoring,
)
from plantain.dashboard.agent.connection import (
    PROVIDER_SPECS,
    AgentConfigurationError,
    AgentConnection,
    AgentProvider,
    load_agent_connection,
)
from plantain.dashboard.agent.models import (
    AgentCapability,
    AgentResponseMetadata,
    DashboardCreationResult,
    IntentDecision,
    IntentRouteResult,
    IntentRoutingContext,
    ScenarioAuthorResult,
    ScenarioOperation,
    ScenarioOperationMatch,
    ScenarioOperationResult,
    UserIntent,
)
from plantain.dashboard.agent.planning import (
    DecisionPlanner,
    DecisionPlanningError,
    supports_decision_planning,
)
from plantain.dashboard.agent.profile import dashboard_agent_environment
from plantain.dashboard.agent.router import IntentRouter
from plantain.dashboard.agent.transport import AgentTransport, AgentTransportError
from plantain.dashboard.agent.ui_models import UiRunEvidence
from plantain.dashboard.agent_usage import AgentUsageRecorder
from plantain.dashboard.agent_usage_types import AgentUsageOperation
from plantain.dashboard.api_inspection_store import (
    DashboardApiInspectionError,
    discard_api_inspection,
    load_api_inspection,
    store_api_inspection,
)
from plantain.dashboard.context_selection import (
    ContextSelection,
    ContextSelectionError,
    select_agent_context,
)
from plantain.dashboard.context_sources import ContextSourceError, ContextSourceKind
from plantain.dashboard.database_evidence import (
    DatabaseEvidenceError,
    load_database_run_evidence,
)
from plantain.dashboard.database_workflow_store import (
    DashboardDatabaseWorkflowError,
    load_database_workflow,
    store_database_workflow,
)
from plantain.dashboard.scenario_catalog import (
    ScenarioCatalogError,
    ScenarioCatalogSearch,
    scenario_id_for_source,
    search_scenario_catalog,
)
from plantain.dashboard.snapshot_context import (
    SnapshotContextError,
    select_verified_snapshot_context,
)
from plantain.dashboard.ui_evidence import UiEvidenceError, load_ui_run_evidence
from plantain.dashboard.ui_workflow_store import (
    DashboardUiWorkflowError,
    UiWorkflowRecord,
    load_ui_workflow,
    store_ui_workflow,
)
from plantain.engine.runtime import ExecutionServices
from plantain.errors import ConfigurationError, PlantainError
from plantain.models.common import StrictModel
from plantain.security.secrets import SecretRegistry

AGENT_PROBE_MAX_OUTPUT_TOKENS = 32
AGENT_PROBE_SYSTEM_PROMPT = (
    "Confirm that this model can return one typed JSON response. "
    "Do not include commentary or private reasoning."
)
AGENT_PROBE_USER_PROMPT = "Return ready as true."
_LOCAL_PROVIDERS = frozenset({AgentProvider.OLLAMA, AgentProvider.LM_STUDIO})


class DashboardAgentError(PlantainError):
    """Raised when the local dashboard cannot safely use its configured agent."""


class _AgentProbe(StrictModel):
    ready: Literal[True]


@dataclass(frozen=True, slots=True)
class _CreationResources:
    transport: AgentTransport
    connection: AgentConnection
    settings: Settings
    services: ExecutionServices
    secrets: SecretRegistry
    selection: ContextSelection
    environ: Mapping[str, str] | None


@dataclass(frozen=True, slots=True)
class _ScenarioAuthoringPreparation:
    authoring: ScenarioAuthorResult | None
    inspection: ApiContractInspection | None
    database_workflow_id: str | None
    ui_workflow_id: str | None


async def route_dashboard_intent(
    project_root: Path,
    intent: UserIntent,
    *,
    context: IntentRoutingContext | None = None,
    environ: Mapping[str, str] | None = None,
) -> IntentRouteResult:
    """Route one intent while keeping prompts and credentials out of browser state."""

    connection = _ready_connection(environ)
    try:
        async with _provider_transport(
            project_root,
            connection,
        ) as (transport, secrets, _services):
            selection = select_agent_context(
                project_root,
                intent.prompt,
                secrets,
            )
            result = await IntentRouter(transport, connection).route(
                intent,
                context=_merge_routing_context(context, selection),
                source_context=selection.packet,
                environ=environ,
            )
            return _sanitized_route(result, secrets)
    except (ContextSelectionError, ContextSourceError) as exc:
        raise DashboardAgentError("Selected context could not be prepared safely.") from exc
    except (AgentTransportError, ConfigurationError) as exc:
        raise DashboardAgentError(
            "The configured agent could not return a valid routing decision."
        ) from exc


async def prepare_dashboard_test(
    project_root: Path,
    intent: UserIntent,
    *,
    context: IntentRoutingContext | None = None,
    environ: Mapping[str, str] | None = None,
) -> DashboardCreationResult:
    """Route one intent and author a validated draft when the workflow supports it."""

    connection = _ready_connection(environ)
    try:
        settings = _effective_settings(Settings.from_env(project_root), connection)
        async with _provider_transport(
            project_root,
            connection,
            settings=settings,
        ) as (transport, secrets, services):
            selection = select_agent_context(
                project_root,
                intent.prompt,
                secrets,
            )
            route = await IntentRouter(transport, connection).route(
                intent,
                context=_merge_routing_context(context, selection),
                source_context=selection.packet,
                environ=environ,
            )
            safe_route = _sanitized_route(route, secrets)
            resources = _CreationResources(
                transport=transport,
                connection=connection,
                settings=settings,
                services=services,
                secrets=secrets,
                selection=selection,
                environ=environ,
            )
            return await _complete_creation(resources, intent, safe_route)
    except (
        ContextSelectionError,
        ContextSourceError,
        SnapshotContextError,
    ) as exc:
        raise DashboardAgentError("Selected context could not be prepared safely.") from exc
    except DashboardApiInspectionError as exc:
        raise DashboardAgentError(
            "The API operation choices could not be retained safely."
        ) from exc
    except (DashboardDatabaseWorkflowError, DashboardUiWorkflowError) as exc:
        raise DashboardAgentError(
            "The progressive authoring workflow could not be retained safely."
        ) from exc
    except ScenarioCatalogError as exc:
        raise DashboardAgentError("Existing local scenarios could not be matched safely.") from exc
    except ApiGroundingError as exc:
        raise DashboardAgentError(
            "The supplied API contract could not be grounded safely."
        ) from exc
    except ScenarioAuthoringError as exc:
        raise DashboardAgentError(
            "The configured agent could not create a valid scenario draft."
        ) from exc
    except DecisionPlanningError as exc:
        raise DashboardAgentError(
            "The configured agent could not create a valid critical test plan."
        ) from exc
    except (AgentTransportError, ConfigurationError) as exc:
        raise DashboardAgentError(
            "The configured agent could not complete this test workflow."
        ) from exc


async def _complete_creation(
    resources: _CreationResources,
    intent: UserIntent,
    route: IntentRouteResult,
) -> DashboardCreationResult:
    decision = route.decision
    if supports_scenario_authoring(decision.capability):
        prepared = await _prepare_scenario_authoring(
            resources,
            intent,
            decision,
        )
        return DashboardCreationResult(
            route=route,
            authoring=prepared.authoring,
            api_inspection=prepared.inspection,
            database_workflow_id=prepared.database_workflow_id,
            ui_workflow_id=prepared.ui_workflow_id,
        )
    if supports_decision_planning(decision.capability):
        snapshot_context = await asyncio.to_thread(
            select_verified_snapshot_context,
            resources.settings.snapshots_dir,
            intent.prompt,
            resources.secrets,
        )
        planning = await DecisionPlanner(
            resources.transport,
            resources.connection,
            resources.secrets,
        ).plan(
            intent,
            decision,
            source_context=resources.selection.packet,
            snapshot_context=snapshot_context,
            environ=resources.environ,
        )
        return DashboardCreationResult(route=route, planning=planning)
    if decision.scenario_operation is None:
        return DashboardCreationResult(route=route)
    scenario_operation = await _prepare_scenario_operation(
        resources.settings,
        decision,
    )
    return DashboardCreationResult(
        route=route,
        scenario_operation=scenario_operation,
    )


async def _prepare_scenario_authoring(
    resources: _CreationResources,
    intent: UserIntent,
    decision: IntentDecision,
) -> _ScenarioAuthoringPreparation:
    evidence, inspection = await _contract_authoring_context(
        resources,
        intent,
        decision,
    )
    if inspection is not None:
        return _ScenarioAuthoringPreparation(None, inspection, None, None)
    authoring = await ScenarioAuthor(
        resources.transport,
        resources.connection,
        resources.settings,
        resources.secrets,
    ).author(
        intent,
        decision,
        source_context=resources.selection.packet,
        api_contract=evidence,
        environ=resources.environ,
    )
    database_workflow_id = (
        store_database_workflow(intent, decision)
        if decision.capability is AgentCapability.DATABASE_DISCOVERY
        else None
    )
    ui_workflow_id = (
        store_ui_workflow(intent, decision)
        if decision.capability is AgentCapability.UI_DISCOVERY
        else None
    )
    return _ScenarioAuthoringPreparation(
        authoring,
        None,
        database_workflow_id,
        ui_workflow_id,
    )


async def _contract_authoring_context(
    resources: _CreationResources,
    intent: UserIntent,
    decision: IntentDecision,
) -> tuple[ApiContractEvidence | None, ApiContractInspection | None]:
    if decision.api_workflow is not ApiWorkflowKind.CONTRACT:
        return None, None
    if decision.api_schema_reference is None:
        _require_attached_api_contract(resources.selection)
        return None, None
    grounding = await inspect_api_contract(
        resources.services,
        resources.secrets,
        intent,
        decision,
        environ=resources.environ,
    )
    inspection = grounding.inspection
    if inspection is not None and inspection.operations:
        inspection_id = store_api_inspection(inspection, intent, decision)
        inspection = inspection.model_copy(update={"inspection_id": inspection_id})
    return grounding.evidence, inspection


def _require_attached_api_contract(selection: ContextSelection) -> None:
    if ContextSourceKind.API_CONTRACT not in selection.available_kinds:
        raise ApiGroundingError("API contract work requires an explicit schema source")


async def _prepare_scenario_operation(
    settings: Settings,
    decision: IntentDecision,
) -> ScenarioOperationResult:
    query = decision.scenario_query
    operation = decision.scenario_operation
    if query is None or operation is None:
        raise DashboardAgentError("Agent routing did not identify an existing scenario safely.")
    search = await asyncio.to_thread(
        search_scenario_catalog,
        settings.project_root,
        query,
    )
    return _scenario_operation_result(operation, query, search)


async def author_dashboard_api_operation(
    project_root: Path,
    inspection_id: str,
    operation_key: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> ScenarioAuthorResult:
    """Author a scenario for one explicitly selected, locally verified operation."""

    connection = _ready_connection(environ)
    try:
        record = load_api_inspection(inspection_id, operation_key)
        settings = _effective_settings(Settings.from_env(project_root), connection)
        async with _provider_transport(
            project_root,
            connection,
            settings=settings,
        ) as (transport, secrets, services):
            selection = select_agent_context(
                project_root,
                record.intent.prompt,
                secrets,
            )
            evidence = await ground_cached_api_operation(
                services,
                secrets,
                record.schema_id,
                operation_key,
            )
            result = await ScenarioAuthor(
                transport,
                connection,
                settings,
                secrets,
            ).author(
                record.intent,
                record.decision,
                source_context=selection.packet,
                api_contract=evidence,
                environ=environ,
            )
    except DashboardApiInspectionError as exc:
        raise DashboardAgentError("The selected API operation is no longer available.") from exc
    except (ContextSelectionError, ContextSourceError) as exc:
        raise DashboardAgentError("Selected context could not be prepared safely.") from exc
    except ApiGroundingError as exc:
        raise DashboardAgentError(
            "The selected API operation could not be grounded safely."
        ) from exc
    except ScenarioAuthoringError as exc:
        raise DashboardAgentError(
            "The configured agent could not create a valid scenario draft."
        ) from exc
    except (AgentTransportError, ConfigurationError) as exc:
        raise DashboardAgentError(
            "The configured agent could not complete this API workflow."
        ) from exc
    discard_api_inspection(inspection_id)
    return result


async def author_dashboard_database_continuation(
    project_root: Path,
    workflow_id: str,
    run_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> ScenarioAuthorResult:
    """Continue one bound workflow from a passed, verified discovery run."""

    connection = _ready_connection(environ)
    try:
        evidence = await asyncio.to_thread(
            load_database_run_evidence,
            project_root,
            run_id,
        )
        scenario_id = scenario_id_for_source(evidence.source_path)
        record = load_database_workflow(workflow_id, scenario_id)
        settings = _effective_settings(Settings.from_env(project_root), connection)
        async with _provider_transport(
            project_root,
            connection,
            settings=settings,
        ) as (transport, secrets, _services):
            selection = select_agent_context(
                project_root,
                record.intent.prompt,
                secrets,
            )
            result = await ScenarioAuthor(
                transport,
                connection,
                settings,
                secrets,
            ).author(
                record.intent,
                record.decision,
                source_context=selection.packet,
                database_evidence=evidence,
                environ=environ,
            )
    except DatabaseEvidenceError as exc:
        raise DashboardAgentError(
            "The selected database discovery evidence is unavailable."
        ) from exc
    except DashboardDatabaseWorkflowError as exc:
        raise DashboardAgentError("This database workflow is no longer available.") from exc
    except (ContextSelectionError, ContextSourceError) as exc:
        raise DashboardAgentError("Selected context could not be prepared safely.") from exc
    except ScenarioAuthoringError as exc:
        raise DashboardAgentError(
            "The configured agent could not create a valid database draft."
        ) from exc
    except (AgentTransportError, ConfigurationError) as exc:
        raise DashboardAgentError(
            "The configured agent could not complete this database workflow."
        ) from exc
    return result


async def author_dashboard_ui_continuation(
    project_root: Path,
    workflow_id: str,
    run_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> ScenarioAuthorResult:
    """Continue one bound UI workflow from exact run evidence."""

    connection = _ready_connection(environ)
    evidence, record = await asyncio.to_thread(
        _load_ui_continuation,
        project_root,
        workflow_id,
        run_id,
    )
    try:
        return await _author_ui_continuation(
            project_root,
            connection,
            evidence,
            record,
            environ,
        )
    except (ContextSelectionError, ContextSourceError) as exc:
        raise DashboardAgentError("Selected context could not be prepared safely.") from exc
    except ScenarioAuthoringError as exc:
        raise DashboardAgentError(
            "The configured agent could not create a valid UI draft."
        ) from exc
    except (AgentTransportError, ConfigurationError) as exc:
        raise DashboardAgentError(
            "The configured agent could not complete this UI workflow."
        ) from exc


def _load_ui_continuation(
    project_root: Path,
    workflow_id: str,
    run_id: str,
) -> tuple[UiRunEvidence, UiWorkflowRecord]:
    try:
        evidence = load_ui_run_evidence(project_root, run_id)
        scenario_id = scenario_id_for_source(evidence.source_path)
        return evidence, load_ui_workflow(workflow_id, scenario_id)
    except UiEvidenceError as exc:
        raise DashboardAgentError("The selected UI discovery evidence is unavailable.") from exc
    except DashboardUiWorkflowError as exc:
        raise DashboardAgentError("This UI discovery workflow is no longer available.") from exc


async def _author_ui_continuation(
    project_root: Path,
    connection: AgentConnection,
    evidence: UiRunEvidence,
    record: UiWorkflowRecord,
    environ: Mapping[str, str] | None,
) -> ScenarioAuthorResult:
    settings = _effective_settings(Settings.from_env(project_root), connection)
    async with _provider_transport(
        project_root,
        connection,
        settings=settings,
    ) as (transport, secrets, _services):
        selection = select_agent_context(
            project_root,
            record.intent.prompt,
            secrets,
        )
        return await ScenarioAuthor(
            transport,
            connection,
            settings,
            secrets,
        ).author(
            record.intent,
            record.decision,
            source_context=selection.packet,
            ui_evidence=evidence,
            environ=environ,
        )


def _scenario_operation_result(
    operation: ScenarioOperation,
    query: str,
    search: ScenarioCatalogSearch,
) -> ScenarioOperationResult:
    matches = [
        ScenarioOperationMatch(
            scenario_id=item.scenario_id,
            name=item.name,
            source=item.source,
            status=item.status,
            step_count=item.step_count,
            domains=list(item.domains),
            tags=list(item.tags),
            issue=item.issue,
        )
        for item in search.items
    ]
    return ScenarioOperationResult(
        operation=operation,
        query=query,
        matches=matches,
        total_matches=search.total_matches,
        matches_limited=search.matches_limited,
    )


def _merge_routing_context(
    context: IntentRoutingContext | None,
    selection: ContextSelection,
) -> IntentRoutingContext:
    readiness = context or IntentRoutingContext()
    kinds = selection.available_kinds
    return readiness.model_copy(
        update={
            "requirements_available": (
                readiness.requirements_available or ContextSourceKind.REQUIREMENTS in kinds
            ),
            "application_source_available": (
                readiness.application_source_available or ContextSourceKind.APPLICATION in kinds
            ),
            "api_schema_available": (
                readiness.api_schema_available or ContextSourceKind.API_CONTRACT in kinds
            ),
        }
    )


async def check_dashboard_agent(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AgentResponseMetadata:
    """Perform a minimal typed provider call without retaining generated content."""

    connection = _ready_connection(environ)
    try:
        async with _provider_transport(
            project_root,
            connection,
        ) as (transport, _secrets, _services):
            _, metadata = await transport.complete_json(
                connection,
                operation=AgentUsageOperation.CONNECTION_CHECK,
                system_prompt=AGENT_PROBE_SYSTEM_PROMPT,
                user_prompt=AGENT_PROBE_USER_PROMPT,
                response_model=_AgentProbe,
                max_output_tokens=AGENT_PROBE_MAX_OUTPUT_TOKENS,
                environ=environ,
            )
            return metadata
    except (AgentTransportError, ConfigurationError) as exc:
        raise DashboardAgentError(
            "The configured agent did not complete its connection check."
        ) from exc


def _ready_connection(environ: Mapping[str, str] | None) -> AgentConnection:
    try:
        connection = load_agent_connection(
            dashboard_agent_environment() if environ is None else environ
        )
    except AgentConfigurationError as exc:
        raise DashboardAgentError("Agent configuration is invalid.") from exc
    if not connection.ready:
        raise DashboardAgentError("Agent setup is incomplete.")
    return connection


@asynccontextmanager
async def _provider_transport(
    project_root: Path,
    connection: AgentConnection,
    *,
    settings: Settings | None = None,
) -> AsyncIterator[tuple[AgentTransport, SecretRegistry, ExecutionServices]]:
    effective_settings = settings or _effective_settings(
        Settings.from_env(project_root),
        connection,
    )
    secrets = SecretRegistry(effective_settings.sensitive_key_names)
    services = ExecutionServices(effective_settings, secrets)
    failed = True
    cleanup_error: ExceptionGroup | None = None
    try:
        yield (
            AgentTransport(
                await services.api(),
                secrets,
                AgentUsageRecorder(effective_settings.output_dir),
            ),
            secrets,
            services,
        )
        failed = False
    finally:
        try:
            await services.close(failed=failed)
        except ExceptionGroup as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            raise DashboardAgentError("Agent resources could not close safely.") from cleanup_error


def _effective_settings(
    settings: Settings,
    connection: AgentConnection,
) -> Settings:
    provider = connection.provider
    if provider not in _LOCAL_PROVIDERS:
        return settings
    expected = PROVIDER_SPECS[provider].base_url
    if connection.base_url != expected:
        raise DashboardAgentError("The local agent endpoint is invalid.")
    return replace(
        settings,
        environment="local",
        network_mode="restricted",
        allow_private_networks=True,
        allowed_hosts=("127.0.0.1",),
        allow_insecure_local_http=True,
        egress_control_enforced=False,
    )


def _sanitized_route(
    result: IntentRouteResult,
    secrets: SecretRegistry,
) -> IntentRouteResult:
    try:
        return IntentRouteResult.model_validate(secrets.redact(result.model_dump(mode="python")))
    except ValidationError as exc:
        raise DashboardAgentError("Agent routing evidence could not be sanitized safely.") from exc


__all__ = [
    "DashboardAgentError",
    "author_dashboard_database_continuation",
    "author_dashboard_ui_continuation",
    "check_dashboard_agent",
    "prepare_dashboard_test",
    "route_dashboard_intent",
]
