"""Automatic intent routing stays typed, bounded, and implementation-neutral."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from plantain.dashboard.agent.connection import (
    AGENT_MODEL_ENV,
    AGENT_PROVIDER_ENV,
    AgentProvider,
    load_agent_connection,
)
from plantain.dashboard.agent.models import (
    AgentCapability,
    AgentResponseMetadata,
    AgentTokenUsage,
    IntentAction,
    IntentDecision,
    IntentRoutingContext,
    UserIntent,
)
from plantain.dashboard.agent.router import IntentRouter
from plantain.dashboard.context_selection import (
    AgentContextExcerpt,
    AgentContextPacket,
)
from plantain.dashboard.context_sources import ContextSourceKind

INPUT_TOKENS = 3
OUTPUT_TOKENS = 2
TOTAL_TOKENS = 5


class FakeTransport:
    """Return one prepared route while recording the ephemeral request."""

    def __init__(self, decision: IntentDecision) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, _connection: Any, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append(kwargs)
        return (
            self.decision,
            AgentResponseMetadata(
                provider=AgentProvider.OLLAMA,
                model="local-model",
                finish_reason="stop",
                usage=AgentTokenUsage(
                    input_tokens=INPUT_TOKENS,
                    output_tokens=OUTPUT_TOKENS,
                    total_tokens=TOTAL_TOKENS,
                ),
            ),
        )


def _connection() -> Any:
    return load_agent_connection(
        {
            AGENT_PROVIDER_ENV: "ollama",
            AGENT_MODEL_ENV: "local-model",
        }
    )


def test_router_selects_internal_capability_without_retaining_prompt() -> None:
    decision = IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.UI_DISCOVERY,
        summary="Inspect the application before generating automation.",
        plan_steps=[
            "Open the approved application target.",
            "Capture semantic evidence and ground resilient locators.",
        ],
    )
    transport = FakeTransport(decision)
    router = IntentRouter(cast("Any", transport), _connection())
    intent = UserIntent(prompt="Test my checkout flow and repair uncertain selectors.")
    source_context = AgentContextPacket(
        source_count=1,
        excerpts=[
            AgentContextExcerpt(
                source_kind=ContextSourceKind.REQUIREMENTS,
                source_label="checkout.md",
                relative_path="checkout.md",
                content="Checkout requires one in-stock product.",
            )
        ],
    )

    result = asyncio.run(
        router.route(
            intent,
            context=IntentRoutingContext(
                canonical_ui_evidence_available=False,
            ),
            source_context=source_context,
        )
    )

    assert result.decision.capability is AgentCapability.UI_DISCOVERY
    assert result.response.usage is not None
    assert result.response.usage.total_tokens == TOTAL_TOKENS
    assert intent.prompt not in repr(result)
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["response_model"] is IntentDecision
    assert call["max_output_tokens"] > 0
    assert '"canonicalUiEvidenceAvailable":false' in call["user_prompt"]
    assert "Checkout requires one in-stock product." in call["user_prompt"]
    assert intent.prompt in call["user_prompt"]
    assert "choose a skill" in call["system_prompt"].casefold()
    assert "permanently prohibited" in call["system_prompt"].casefold()
    assert "untrusted evidence" in call["system_prompt"].casefold()
    assert "in-stock product" not in repr(result)


def test_router_preserves_one_typed_clarification() -> None:
    decision = IntentDecision(
        action=IntentAction.CLARIFY,
        summary="The API contract target is missing.",
        question="What Swagger or OpenAPI URL should Plantain inspect?",
        required_inputs=["Swagger or OpenAPI URL"],
    )
    transport = FakeTransport(decision)
    router = IntentRouter(cast("Any", transport), _connection())

    result = asyncio.run(router.route(UserIntent(prompt="Create contract tests for my service.")))

    assert result.decision.action is IntentAction.CLARIFY
    assert result.decision.question == ("What Swagger or OpenAPI URL should Plantain inspect?")
    assert result.decision.plan_steps == []
