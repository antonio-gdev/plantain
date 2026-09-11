"""Progressive UI creation keeps intent server-side and evidence scenario-bound."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from plantain.config import Settings
from plantain.dashboard.agent import runtime
from plantain.dashboard.agent.connection import AgentProvider
from plantain.dashboard.agent.models import (
    AgentCapability,
    AgentResponseMetadata,
    IntentAction,
    IntentDecision,
    IntentRouteResult,
    ScenarioAuthorResult,
    ScenarioDraft,
    UserIntent,
)
from plantain.dashboard.agent.runtime import DashboardAgentError
from plantain.dashboard.context_selection import AgentContextPacket, ContextSelection
from plantain.dashboard.scenario_catalog import scenario_id_for_source
from plantain.dashboard.ui_evidence import UiEvidenceError
from plantain.dashboard.ui_workflow_store import DashboardUiWorkflowError

WORKFLOW_ID = "a" * 32
RUN_ID = "b" * 32
SOURCE_PATH = "generated/ui/discover-checkout.yaml"


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


def _decision() -> IntentDecision:
    return IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.UI_DISCOVERY,
        summary="Discover the checkout page before extending its test.",
        plan_steps=["Capture current semantic page evidence."],
    )


def _route() -> IntentRouteResult:
    return IntentRouteResult(decision=_decision(), response=_metadata())


def _authoring() -> ScenarioAuthorResult:
    return ScenarioAuthorResult(
        draft=ScenarioDraft(
            scenario="Discover checkout",
            yaml_text=(
                "scenario: Discover checkout\n"
                "outputs:\n"
                "  discoverySnapshot: ${discover.snapshots[0].canonicalFile}\n"
                "steps:\n"
                "  - capturePageSnapshot:\n"
                "      id: discover\n"
                "      url: https://example.test/checkout\n"
            ),
            step_count=1,
            activities=["capturePageSnapshot"],
        ),
        responses=[_metadata()],
    )


def _selection() -> ContextSelection:
    return ContextSelection(
        packet=AgentContextPacket(source_count=0),
        available_kinds=frozenset(),
    )


def test_initial_ui_creation_retains_goal_behind_opaque_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()
    captured: dict[str, Any] = {}

    class Router:
        def __init__(self, *_args: Any) -> None:
            pass

        async def route(self, *_args: Any, **_kwargs: Any) -> IntentRouteResult:
            return _route()

    class Author:
        def __init__(self, *_args: Any) -> None:
            pass

        async def author(self, *_args: Any, **kwargs: Any) -> ScenarioAuthorResult:
            captured["author"] = kwargs
            return _authoring()

    def store(intent: UserIntent, decision: IntentDecision) -> str:
        captured["intent"] = intent
        captured["decision"] = decision
        return WORKFLOW_ID

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)
    monkeypatch.setattr(runtime, "ScenarioAuthor", Author)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())
    monkeypatch.setattr(runtime, "store_ui_workflow", store)

    intent = UserIntent(prompt="Discover and test the checkout journey.")
    result = asyncio.run(
        runtime.prepare_dashboard_test(
            tmp_path,
            intent,
            environ=_environment(),
        )
    )

    assert result.authoring is not None
    assert result.ui_workflow_id == WORKFLOW_ID
    assert result.database_workflow_id is None
    assert captured["intent"] == intent
    assert captured["decision"].capability is AgentCapability.UI_DISCOVERY
    assert captured["author"].get("ui_evidence") is None
    assert _Services.instances[0].closed == [False]


def test_ui_continuation_proves_bound_scenario_before_authoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()
    captured: dict[str, Any] = {}
    evidence = SimpleNamespace(source_path=SOURCE_PATH)
    intent = UserIntent(prompt="Discover and test the checkout journey.")
    decision = _decision()
    expected_scenario_id = scenario_id_for_source(SOURCE_PATH)

    class Author:
        def __init__(self, *_args: Any) -> None:
            pass

        async def author(self, *_args: Any, **kwargs: Any) -> ScenarioAuthorResult:
            captured["author"] = kwargs
            return _authoring()

    def load_workflow(workflow_id: str, scenario_id: str) -> SimpleNamespace:
        captured["workflow"] = (workflow_id, scenario_id)
        return SimpleNamespace(intent=intent, decision=decision)

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "ScenarioAuthor", Author)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())
    monkeypatch.setattr(runtime, "load_ui_run_evidence", lambda *_args: evidence)
    monkeypatch.setattr(runtime, "load_ui_workflow", load_workflow)

    result = asyncio.run(
        runtime.author_dashboard_ui_continuation(
            tmp_path,
            WORKFLOW_ID,
            RUN_ID,
            environ=_environment(),
        )
    )

    assert result.draft.activities == ["capturePageSnapshot"]
    assert captured["workflow"] == (WORKFLOW_ID, expected_scenario_id)
    assert captured["author"]["ui_evidence"] is evidence
    assert captured["author"]["source_context"].source_count == 0
    assert _Services.instances[0].closed == [False]


def test_ui_continuation_translates_invalid_evidence_without_private_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_detail = "synthetic-private-ui-evidence-detail"

    def reject(*_args: Any) -> None:
        raise UiEvidenceError(private_detail)

    monkeypatch.setattr(runtime, "load_ui_run_evidence", reject)

    with pytest.raises(DashboardAgentError, match="evidence is unavailable") as captured:
        asyncio.run(
            runtime.author_dashboard_ui_continuation(
                tmp_path,
                WORKFLOW_ID,
                RUN_ID,
                environ=_environment(),
            )
        )

    assert private_detail not in str(captured.value)
    assert private_detail not in repr(captured.value)


def test_ui_continuation_translates_workflow_mismatch_without_private_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_detail = "synthetic-private-ui-workflow-detail"
    evidence = SimpleNamespace(source_path=SOURCE_PATH)

    def reject(*_args: Any) -> None:
        raise DashboardUiWorkflowError(private_detail)

    monkeypatch.setattr(runtime, "load_ui_run_evidence", lambda *_args: evidence)
    monkeypatch.setattr(runtime, "load_ui_workflow", reject)

    with pytest.raises(DashboardAgentError, match="no longer available") as captured:
        asyncio.run(
            runtime.author_dashboard_ui_continuation(
                tmp_path,
                WORKFLOW_ID,
                RUN_ID,
                environ=_environment(),
            )
        )

    assert private_detail not in str(captured.value)
    assert private_detail not in repr(captured.value)
