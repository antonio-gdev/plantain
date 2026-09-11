"""Combined dashboard routing and scenario-authoring lifecycle coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest

from plantain.config import Settings
from plantain.dashboard.agent import runtime
from plantain.dashboard.agent.api_models import ApiWorkflowKind
from plantain.dashboard.agent.authoring import ScenarioAuthoringError
from plantain.dashboard.agent.connection import AgentProvider
from plantain.dashboard.agent.models import (
    AgentCapability,
    AgentResponseMetadata,
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
    UserIntent,
)
from plantain.dashboard.agent.planning import DecisionPlanningError
from plantain.dashboard.agent.runtime import DashboardAgentError
from plantain.dashboard.context_selection import AgentContextPacket, ContextSelection
from plantain.dashboard.scenario_catalog import (
    ScenarioCatalogItem,
    ScenarioCatalogSearch,
)
from plantain.dashboard.snapshot_context import (
    SnapshotContextExcerpt,
    SnapshotContextPacket,
)

SNAPSHOT_EVIDENCE_ID = f"snapshot-{'c' * 20}"
SCENARIO_ID = "a" * 64


class _Services:
    instances: ClassVar[list[_Services]] = []

    def __init__(self, settings: Settings, secrets: Any) -> None:
        self.settings = settings
        self.secrets = secrets
        self.closed: list[bool] = []
        self.instances.append(self)

    async def api(self) -> object:
        return object()

    async def close(self, *, failed: bool) -> None:
        self.closed.append(failed)


def _environment() -> dict[str, str]:
    return {
        "PLANTAIN_AGENT_PROVIDER": "ollama",
        "PLANTAIN_AGENT_MODEL": "local-model",
    }


def _metadata() -> AgentResponseMetadata:
    return AgentResponseMetadata(
        provider=AgentProvider.OLLAMA,
        model="local-model",
        finish_reason="stop",
    )


def _route(
    *,
    action: IntentAction = IntentAction.PLAN,
    capability: AgentCapability | None = AgentCapability.API_CONTRACT,
    operation: ScenarioOperation | None = None,
    query: str | None = None,
) -> IntentRouteResult:
    return IntentRouteResult(
        decision=IntentDecision(
            action=action,
            capability=capability,
            summary="Create a contract-grounded API test.",
            question="Which API contract should Plantain use?"
            if action is IntentAction.CLARIFY
            else None,
            required_inputs=["Swagger or OpenAPI URL"] if action is IntentAction.CLARIFY else [],
            plan_steps=["Create a validated scenario."] if action is IntentAction.PLAN else [],
            scenario_operation=operation,
            scenario_query=query,
            api_workflow=(
                ApiWorkflowKind.REQUEST
                if action is IntentAction.PLAN and capability is AgentCapability.API_CONTRACT
                else None
            ),
        ),
        response=_metadata(),
    )


def _authoring() -> ScenarioAuthorResult:
    return ScenarioAuthorResult(
        draft=ScenarioDraft(
            scenario="Generated API test",
            yaml_text=(
                "scenario: Generated API test\n"
                "steps:\n"
                "  - sendRequest:\n"
                "      id: request\n"
                "      endpoint: https://api.example.test/health\n"
                "      method: GET\n"
            ),
            step_count=1,
            activities=["sendRequest"],
        ),
        responses=[_metadata()],
    )


def _planning() -> DecisionPlanAuthorResult:
    return DecisionPlanAuthorResult(
        draft=DecisionPlanDraft(
            feature="Checkout",
            sources=[
                DecisionEvidenceSource(
                    evidence_id=SNAPSHOT_EVIDENCE_ID,
                    kind=DecisionEvidenceKind.VERIFIED_UI,
                    label="Checkout",
                    reference="checkout.semantic.json",
                )
            ],
            assumptions=[],
            gaps=[],
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
                    objective="Verify the observed checkout path.",
                    priority=DecisionPriority.CRITICAL,
                    dimensions=[DecisionDimensionKind.CRITICAL_PATH],
                    actions=["Follow the observed flow."],
                    expected_results=["Checkout completes."],
                    evidence_ids=[SNAPSHOT_EVIDENCE_ID],
                )
            ],
        ),
        responses=[_metadata()],
    )


def _snapshot_context() -> SnapshotContextPacket:
    return SnapshotContextPacket(
        source_count=1,
        excerpts=[
            SnapshotContextExcerpt(
                evidence_id=SNAPSHOT_EVIDENCE_ID,
                canonical_file="checkout.semantic.json",
                activities=["checkout"],
                url="https://example.test/checkout",
                page_title="Checkout",
                normalized_key="example.test/checkout",
                content='- button "Checkout"\n',
            )
        ],
    )


def _selection() -> ContextSelection:
    return ContextSelection(
        packet=AgentContextPacket(source_count=0),
        available_kinds=frozenset(),
    )


def test_prepare_routes_and_authors_in_one_resource_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()
    calls: dict[str, Any] = {}

    class Router:
        def __init__(self, transport: Any, connection: Any) -> None:
            calls["router_transport"] = transport
            calls["router_connection"] = connection

        async def route(self, *_args: Any, **kwargs: Any) -> IntentRouteResult:
            calls["route"] = kwargs
            return _route()

    class Author:
        def __init__(
            self,
            transport: Any,
            connection: Any,
            settings: Settings,
            secrets: Any,
        ) -> None:
            calls["author_transport"] = transport
            calls["author_connection"] = connection
            calls["author_settings"] = settings
            calls["author_secrets"] = secrets

        async def author(
            self,
            *_args: Any,
            **kwargs: Any,
        ) -> ScenarioAuthorResult:
            calls["author"] = kwargs
            return _authoring()

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)
    monkeypatch.setattr(runtime, "ScenarioAuthor", Author)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())

    result = asyncio.run(
        runtime.prepare_dashboard_test(
            tmp_path,
            UserIntent(prompt="Create an API health test."),
            environ=_environment(),
        )
    )

    assert result.authoring is not None
    assert result.authoring.draft.scenario == "Generated API test"
    assert calls["router_transport"] is calls["author_transport"]
    assert calls["router_connection"] is calls["author_connection"]
    assert calls["author_secrets"] is _Services.instances[0].secrets
    assert calls["author"]["source_context"].source_count == 0
    assert len(_Services.instances) == 1
    assert _Services.instances[0].closed == [False]


def test_prepare_resolves_existing_scenario_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()
    calls: dict[str, Any] = {}

    class Router:
        def __init__(self, *_args: Any) -> None:
            pass

        async def route(self, *_args: Any, **_kwargs: Any) -> IntentRouteResult:
            return _route(
                capability=AgentCapability.SCENARIO_EXECUTION,
                operation=ScenarioOperation.RUN,
                query="checkout",
            )

    def search(root: Path, query: str) -> ScenarioCatalogSearch:
        calls["search"] = (root, query)
        item = ScenarioCatalogItem(
            scenario_id=SCENARIO_ID,
            name="Checkout",
            source="ui/checkout.yaml",
            status="ready",
            step_count=3,
            domains=("UI",),
            tags=("smoke",),
            additional_tag_count=0,
            issue="",
        )
        return ScenarioCatalogSearch(
            items=(item,),
            total_matches=1,
            matches_limited=False,
        )

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())
    monkeypatch.setattr(runtime, "search_scenario_catalog", search)

    result = asyncio.run(
        runtime.prepare_dashboard_test(
            tmp_path,
            UserIntent(prompt="Run checkout."),
            environ=_environment(),
        )
    )

    assert result.authoring is None
    assert result.planning is None
    assert result.scenario_operation is not None
    assert result.scenario_operation.operation is ScenarioOperation.RUN
    assert result.scenario_operation.matches[0].scenario_id == SCENARIO_ID
    assert calls["search"] == (tmp_path, "checkout")
    assert _Services.instances[0].closed == [False]


def test_prepare_returns_clarification_without_invoking_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()

    class Router:
        def __init__(self, *_args: Any) -> None:
            pass

        async def route(self, *_args: Any, **_kwargs: Any) -> IntentRouteResult:
            return _route(action=IntentAction.CLARIFY, capability=None)

    class Author:
        def __init__(self, *_args: Any) -> None:
            raise AssertionError("clarification must not author a scenario")

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)
    monkeypatch.setattr(runtime, "ScenarioAuthor", Author)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())

    result = asyncio.run(
        runtime.prepare_dashboard_test(
            tmp_path,
            UserIntent(prompt="Create an API test."),
            environ=_environment(),
        )
    )

    assert result.route.decision.action is IntentAction.CLARIFY
    assert result.authoring is None
    assert _Services.instances[0].closed == [False]


def test_prepare_translates_author_failure_and_marks_cleanup_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()

    class Router:
        def __init__(self, *_args: Any) -> None:
            pass

        async def route(self, *_args: Any, **_kwargs: Any) -> IntentRouteResult:
            return _route()

    class Author:
        def __init__(self, *_args: Any) -> None:
            pass

        async def author(self, *_args: Any, **_kwargs: Any) -> ScenarioAuthorResult:
            raise ScenarioAuthoringError("synthetic private detail")

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)
    monkeypatch.setattr(runtime, "ScenarioAuthor", Author)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())

    with pytest.raises(DashboardAgentError, match="valid scenario draft") as captured:
        asyncio.run(
            runtime.prepare_dashboard_test(
                tmp_path,
                UserIntent(prompt="Create an API test."),
                environ=_environment(),
            )
        )

    assert "synthetic private detail" not in str(captured.value)
    assert _Services.instances[0].closed == [True]


def test_prepare_routes_and_plans_in_one_resource_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()
    calls: dict[str, Any] = {}

    class Router:
        def __init__(self, transport: Any, connection: Any) -> None:
            calls["router_transport"] = transport
            calls["router_connection"] = connection

        async def route(self, *_args: Any, **_kwargs: Any) -> IntentRouteResult:
            return _route(capability=AgentCapability.CRITICAL_DECISION_SPACE)

    class Planner:
        def __init__(
            self,
            transport: Any,
            connection: Any,
            secrets: Any,
        ) -> None:
            calls["planner_transport"] = transport
            calls["planner_connection"] = connection
            calls["planner_secrets"] = secrets

        async def plan(
            self,
            *_args: Any,
            **kwargs: Any,
        ) -> DecisionPlanAuthorResult:
            calls["plan"] = kwargs
            return _planning()

    def snapshots(path: Path, query: str, secrets: Any) -> SnapshotContextPacket:
        calls["snapshot_path"] = path
        calls["snapshot_query"] = query
        calls["snapshot_secrets"] = secrets
        return _snapshot_context()

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)
    monkeypatch.setattr(runtime, "DecisionPlanner", Planner)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())
    monkeypatch.setattr(runtime, "select_verified_snapshot_context", snapshots)

    result = asyncio.run(
        runtime.prepare_dashboard_test(
            tmp_path,
            UserIntent(prompt="Find the critical checkout test space."),
            environ=_environment(),
        )
    )

    assert result.authoring is None
    assert result.planning is not None
    assert result.planning.draft.feature == "Checkout"
    assert calls["router_transport"] is calls["planner_transport"]
    assert calls["router_connection"] is calls["planner_connection"]
    assert calls["planner_secrets"] is _Services.instances[0].secrets
    assert calls["snapshot_secrets"] is _Services.instances[0].secrets
    assert calls["snapshot_path"] == _Services.instances[0].settings.snapshots_dir
    assert calls["snapshot_query"] == "Find the critical checkout test space."
    assert calls["plan"]["source_context"].source_count == 0
    assert calls["plan"]["snapshot_context"].source_count == 1
    assert _Services.instances[0].closed == [False]


def test_prepare_translates_planner_failure_and_marks_cleanup_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()

    class Router:
        def __init__(self, *_args: Any) -> None:
            pass

        async def route(self, *_args: Any, **_kwargs: Any) -> IntentRouteResult:
            return _route(capability=AgentCapability.CRITICAL_DECISION_SPACE)

    class Planner:
        def __init__(self, *_args: Any) -> None:
            pass

        async def plan(self, *_args: Any, **_kwargs: Any) -> DecisionPlanAuthorResult:
            raise DecisionPlanningError("synthetic private planning detail")

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)
    monkeypatch.setattr(runtime, "DecisionPlanner", Planner)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())
    monkeypatch.setattr(
        runtime,
        "select_verified_snapshot_context",
        lambda *_args: _snapshot_context(),
    )

    with pytest.raises(DashboardAgentError, match="valid critical test plan") as captured:
        asyncio.run(
            runtime.prepare_dashboard_test(
                tmp_path,
                UserIntent(prompt="Find the critical checkout test space."),
                environ=_environment(),
            )
        )

    assert "synthetic private planning detail" not in str(captured.value)
    assert _Services.instances[0].closed == [True]
