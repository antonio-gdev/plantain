"""Dashboard agent lifecycle and local-provider policy coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
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
    UserIntent,
)
from plantain.dashboard.agent.runtime import DashboardAgentError
from plantain.dashboard.agent.transport import AgentTransportError
from plantain.dashboard.context_selection import (
    AgentContextExcerpt,
    AgentContextPacket,
    ContextSelection,
    ContextSelectionError,
)
from plantain.dashboard.context_sources import ContextSourceKind


class _Services:
    instances: ClassVar[list[_Services]] = []

    def __init__(self, settings: Settings, _secrets: Any) -> None:
        self.settings = settings
        self.closed: list[bool] = []
        self.instances.append(self)

    async def api(self) -> object:
        return object()

    async def close(self, *, failed: bool) -> None:
        self.closed.append(failed)


def _decision() -> IntentRouteResult:
    return IntentRouteResult(
        decision=IntentDecision(
            action=IntentAction.PLAN,
            capability=AgentCapability.UI_DISCOVERY,
            summary="Inspect the unfamiliar page before generating automation.",
            plan_steps=["Capture semantic UI evidence."],
        ),
        response=AgentResponseMetadata(
            provider=AgentProvider.OLLAMA,
            model="qwen3",
            finish_reason="stop",
        ),
    )


def _local_environment() -> dict[str, str]:
    return {
        "PLANTAIN_AGENT_PROVIDER": "ollama",
        "PLANTAIN_AGENT_MODEL": "qwen3",
    }


def test_route_uses_exact_loopback_policy_and_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()
    calls: list[dict[str, Any]] = []

    class Router:
        def __init__(self, *_args: Any) -> None:
            pass

        async def route(self, *_args: Any, **kwargs: Any) -> IntentRouteResult:
            calls.append(kwargs)
            return _decision()

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)
    monkeypatch.setattr(
        runtime,
        "select_agent_context",
        lambda *_args: ContextSelection(
            packet=AgentContextPacket(
                source_count=1,
                excerpts=[
                    AgentContextExcerpt(
                        source_kind=ContextSourceKind.REQUIREMENTS,
                        source_label="rules.md",
                        relative_path="rules.md",
                        content="Checkout requires inventory.",
                    )
                ],
            ),
            available_kinds=frozenset({ContextSourceKind.REQUIREMENTS}),
        ),
    )

    result = asyncio.run(
        runtime.route_dashboard_intent(
            tmp_path,
            UserIntent(prompt="Test the checkout page"),
            environ=_local_environment(),
        )
    )

    assert result.decision.capability is AgentCapability.UI_DISCOVERY
    assert len(_Services.instances) == 1
    service = _Services.instances[0]
    assert service.settings.network_mode == "restricted"
    assert service.settings.allowed_hosts == ("127.0.0.1",)
    assert service.settings.allow_private_networks is True
    assert service.settings.allow_insecure_local_http is True
    assert service.closed == [False]
    assert calls[0]["context"].requirements_available is True
    assert calls[0]["source_context"].source_count == 1


def test_route_translates_provider_failure_and_marks_cleanup_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()

    class Router:
        def __init__(self, *_args: Any) -> None:
            pass

        async def route(self, *_args: Any, **_kwargs: Any) -> IntentRouteResult:
            raise AgentTransportError("synthetic provider detail")

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "IntentRouter", Router)

    with pytest.raises(DashboardAgentError, match="valid routing decision"):
        asyncio.run(
            runtime.route_dashboard_intent(
                tmp_path,
                UserIntent(prompt="Test the checkout page"),
                environ=_local_environment(),
            )
        )

    assert _Services.instances[0].closed == [True]


def test_incomplete_setup_does_not_create_runtime_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()
    monkeypatch.setattr(runtime, "ExecutionServices", _Services)

    with pytest.raises(DashboardAgentError, match="setup is incomplete"):
        asyncio.run(
            runtime.route_dashboard_intent(
                tmp_path,
                UserIntent(prompt="Test the checkout page"),
                environ={"PLANTAIN_AGENT_PROVIDER": "ollama"},
            )
        )

    assert _Services.instances == []


def test_context_selection_failure_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Services.instances.clear()

    def reject_context(*_args: Any) -> ContextSelection:
        raise ContextSelectionError("synthetic context detail")

    monkeypatch.setattr(runtime, "ExecutionServices", _Services)
    monkeypatch.setattr(runtime, "select_agent_context", reject_context)

    with pytest.raises(DashboardAgentError, match="context could not be prepared"):
        asyncio.run(
            runtime.route_dashboard_intent(
                tmp_path,
                UserIntent(prompt="Test the checkout page"),
                environ=_local_environment(),
            )
        )

    assert _Services.instances[0].closed == [True]
