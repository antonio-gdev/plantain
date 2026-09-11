"""Guided API creation shares the dashboard provider and resource lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest

from plantain.config import Settings
from plantain.dashboard.agent import runtime
from plantain.dashboard.agent.api_grounding import ApiContractGrounding
from plantain.dashboard.agent.api_models import (
    ApiContractEvidence,
    ApiContractInspection,
    ApiOperationCandidate,
    ApiWorkflowKind,
)
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
from plantain.dashboard.api_inspection_store import ApiInspectionRecord
from plantain.dashboard.context_selection import AgentContextPacket, ContextSelection
from plantain.models.api import HttpMethod

INSPECTION_ID = "a" * 32
SCHEMA_ID = "b" * 64
FIRST_OPERATION_KEY = "c" * 64
SECOND_OPERATION_KEY = "d" * 64
SCHEMA_REFERENCE = "env:PETSTORE_SCHEMA_URL"
USER_PROMPT = f"Use {SCHEMA_REFERENCE} to test the pet operations."


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


def _intent() -> UserIntent:
    return UserIntent(prompt=USER_PROMPT)


def _decision() -> IntentDecision:
    return IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.API_CONTRACT,
        summary="Create a contract-grounded pet test.",
        plan_steps=["Inspect the contract.", "Create a validated scenario."],
        api_workflow=ApiWorkflowKind.CONTRACT,
        api_schema_reference=SCHEMA_REFERENCE,
        api_operation_query="pet operations",
    )


def _route() -> IntentRouteResult:
    return IntentRouteResult(decision=_decision(), response=_metadata())


def _selection() -> ContextSelection:
    return ContextSelection(
        packet=AgentContextPacket(source_count=0),
        available_kinds=frozenset(),
    )


def _evidence(operation_key: str = FIRST_OPERATION_KEY) -> ApiContractEvidence:
    return ApiContractEvidence(
        schema_id=SCHEMA_ID,
        operation_key=operation_key,
        content='{"operation":{"operationId":"listPets"}}',
    )


def _inspection() -> ApiContractInspection:
    return ApiContractInspection(
        schema_id=SCHEMA_ID,
        schema_version="3.0.3",
        source_url="https://api.example.test/openapi.json",
        base_url="https://api.example.test",
        query="pet operations",
        operations=[
            ApiOperationCandidate(
                operation_key=FIRST_OPERATION_KEY,
                operation_id="listPets",
                method=HttpMethod.GET,
                path="/pets",
                summary="List pets",
            ),
            ApiOperationCandidate(
                operation_key=SECOND_OPERATION_KEY,
                operation_id="getPet",
                method=HttpMethod.GET,
                path="/pets/{petId}",
                summary="Get one pet",
            ),
        ],
        total_matches=2,
    )


def _authoring() -> ScenarioAuthorResult:
    return ScenarioAuthorResult(
        draft=ScenarioDraft(
            scenario="Generated pet test",
            yaml_text=(
                "scenario: Generated pet test\n"
                "steps:\n"
                "  - loadApiSchema:\n"
                "      id: schema\n"
                f"      url: {SCHEMA_REFERENCE}\n"
            ),
            step_count=1,
            activities=["loadApiSchema"],
        ),
        responses=[_metadata()],
    )


class _Router:
    def __init__(self, *_args: Any) -> None:
        pass

    async def route(self, *_args: Any, **_kwargs: Any) -> IntentRouteResult:
        return _route()


def _install_common(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()
    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", _Router)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())


def test_unique_contract_operation_authors_without_extra_user_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}
    evidence = _evidence()

    async def inspect(*_args: Any, **_kwargs: Any) -> ApiContractGrounding:
        return ApiContractGrounding(evidence=evidence)

    class Author:
        def __init__(self, *_args: Any) -> None:
            pass

        async def author(
            self,
            *_args: Any,
            **kwargs: Any,
        ) -> ScenarioAuthorResult:
            calls["author"] = kwargs
            return _authoring()

    _install_common(monkeypatch)
    monkeypatch.setattr(runtime, "inspect_api_contract", inspect)
    monkeypatch.setattr(runtime, "ScenarioAuthor", Author)

    result = asyncio.run(
        runtime.prepare_dashboard_test(
            tmp_path,
            _intent(),
            environ=_environment(),
        )
    )

    assert result.authoring is not None
    assert result.api_inspection is None
    assert calls["author"]["api_contract"] is evidence
    assert _Services.instances[0].closed == [False]


def test_ambiguous_contract_returns_only_verified_operation_choices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}
    inspection = _inspection()

    async def inspect(*_args: Any, **_kwargs: Any) -> ApiContractGrounding:
        return ApiContractGrounding(inspection=inspection)

    def retain(
        selected: ApiContractInspection,
        intent: UserIntent,
        decision: IntentDecision,
    ) -> str:
        calls["retained"] = (selected, intent, decision)
        return INSPECTION_ID

    class Author:
        def __init__(self, *_args: Any) -> None:
            raise AssertionError("ambiguous operations must not be authored")

    _install_common(monkeypatch)
    monkeypatch.setattr(runtime, "inspect_api_contract", inspect)
    monkeypatch.setattr(runtime, "store_api_inspection", retain)
    monkeypatch.setattr(runtime, "ScenarioAuthor", Author)

    result = asyncio.run(
        runtime.prepare_dashboard_test(
            tmp_path,
            _intent(),
            environ=_environment(),
        )
    )

    assert result.authoring is None
    assert result.api_inspection is not None
    assert result.api_inspection.inspection_id == INSPECTION_ID
    assert result.api_inspection.operations[0].operation_key == FIRST_OPERATION_KEY
    assert calls["retained"] == (inspection, _intent(), _decision())
    assert _Services.instances[0].closed == [False]


def test_selected_operation_is_reverified_authored_and_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}
    evidence = _evidence(SECOND_OPERATION_KEY)
    record = ApiInspectionRecord(
        inspection_id=INSPECTION_ID,
        schema_id=SCHEMA_ID,
        intent=_intent(),
        decision=_decision(),
    )

    async def ground(
        services: Any,
        secrets: Any,
        schema_id: str,
        operation_key: str,
    ) -> ApiContractEvidence:
        calls["ground"] = (services, secrets, schema_id, operation_key)
        return evidence

    class Author:
        def __init__(self, *_args: Any) -> None:
            pass

        async def author(
            self,
            intent: UserIntent,
            decision: IntentDecision,
            **kwargs: Any,
        ) -> ScenarioAuthorResult:
            calls["author"] = (intent, decision, kwargs)
            return _authoring()

    _Services.instances.clear()
    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "select_agent_context", lambda *_args: _selection())
    monkeypatch.setattr(runtime, "load_api_inspection", lambda *_args: record)
    monkeypatch.setattr(runtime, "ground_cached_api_operation", ground)
    monkeypatch.setattr(runtime, "ScenarioAuthor", Author)
    monkeypatch.setattr(
        runtime,
        "discard_api_inspection",
        lambda inspection_id: calls.setdefault("discarded", inspection_id),
    )

    result = asyncio.run(
        runtime.author_dashboard_api_operation(
            tmp_path,
            INSPECTION_ID,
            SECOND_OPERATION_KEY,
            environ=_environment(),
        )
    )

    assert result.draft.scenario == "Generated pet test"
    assert calls["ground"][2:] == (SCHEMA_ID, SECOND_OPERATION_KEY)
    assert calls["author"][0] == _intent()
    assert calls["author"][1] == _decision()
    assert calls["author"][2]["api_contract"] is evidence
    assert calls["discarded"] == INSPECTION_ID
    assert _Services.instances[0].closed == [False]
