"""Evidence-grounded dashboard critical-decision planning coverage."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from plantain.dashboard.agent.connection import (
    AGENT_MODEL_ENV,
    AGENT_PROVIDER_ENV,
    AgentProvider,
    load_agent_connection,
)
from plantain.dashboard.agent.models import (
    AgentCapability,
    AgentResponseMetadata,
    DecisionDimensionKind,
    DecisionEvidenceKind,
    DecisionPlanDimension,
    DecisionPlanTest,
    DecisionPriority,
    GeneratedDecisionPlanPayload,
    IntentAction,
    IntentDecision,
    UserIntent,
)
from plantain.dashboard.agent.planning import (
    DecisionPlanner,
    DecisionPlanningError,
)
from plantain.dashboard.context_selection import (
    AgentContextExcerpt,
    AgentContextPacket,
)
from plantain.dashboard.context_sources import ContextSourceKind
from plantain.dashboard.snapshot_context import (
    SnapshotContextExcerpt,
    SnapshotContextPacket,
    SnapshotElementCount,
)
from plantain.security.secrets import SecretRegistry

EXPECTED_DIMENSION_COUNT = 6
EXPECTED_REPAIR_CALLS = 2
EXPECTED_SOURCE_COUNT = 2
SNAPSHOT_EVIDENCE_ID = f"snapshot-{'a' * 20}"
UNKNOWN_EVIDENCE_ID = f"source-{'b' * 20}"


class _Transport:
    def __init__(self, *payloads: GeneratedDecisionPlanPayload) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        _connection: Any,
        **kwargs: Any,
    ) -> tuple[GeneratedDecisionPlanPayload, AgentResponseMetadata]:
        self.calls.append(kwargs)
        return (
            self._payloads.pop(0),
            AgentResponseMetadata(
                provider=AgentProvider.OLLAMA,
                model="local-model",
                finish_reason="stop",
            ),
        )


def _connection() -> Any:
    return load_agent_connection(
        {
            AGENT_PROVIDER_ENV: "ollama",
            AGENT_MODEL_ENV: "local-model",
        }
    )


def _decision() -> IntentDecision:
    return IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.CRITICAL_DECISION_SPACE,
        summary="Derive the minimum checkout test space.",
        plan_steps=[
            "Compare verified UI states with attached business rules.",
            "Propose evidence-backed critical cases.",
        ],
    )


def _payload(
    *,
    complete: bool = True,
    evidence_id: str = SNAPSHOT_EVIDENCE_ID,
    feature: str = "Checkout",
) -> GeneratedDecisionPlanPayload:
    kinds = list(DecisionDimensionKind)
    if not complete:
        kinds.remove(DecisionDimensionKind.ACCESSIBILITY)
    dimensions = [
        DecisionPlanDimension(
            kind=kind,
            summary=f"Coverage for {kind.value}.",
            evidence_ids=[evidence_id],
        )
        for kind in kinds
    ]
    return GeneratedDecisionPlanPayload(
        feature=feature,
        assumptions=["The verified states represent the current application."],
        gaps=[],
        dimensions=dimensions,
        tests=[
            DecisionPlanTest(
                case_id="checkout-happy-path",
                title="Complete checkout",
                objective="Verify the evidence-backed checkout path.",
                priority=DecisionPriority.CRITICAL,
                dimensions=[
                    DecisionDimensionKind.CRITICAL_PATH,
                    DecisionDimensionKind.STATE_TRANSITION,
                ],
                preconditions=["An item is available."],
                actions=["Follow the observed checkout flow."],
                expected_results=["Checkout reaches its confirmed state."],
                evidence_ids=[evidence_id],
            )
        ],
    )


def _source_context() -> AgentContextPacket:
    return AgentContextPacket(
        source_count=1,
        excerpts=[
            AgentContextExcerpt(
                source_kind=ContextSourceKind.REQUIREMENTS,
                source_label="Checkout requirements",
                relative_path="requirements/checkout.md",
                content="Checkout requires one available inventory item.",
            )
        ],
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
                element_counts=[
                    SnapshotElementCount(name="button", count=1),
                ],
                key_ids=["checkout-button"],
                content='- button "Checkout"\n',
            )
        ],
    )


def test_planner_combines_evidence_and_returns_browser_safe_projection() -> None:
    transport = _Transport(_payload())
    planner = DecisionPlanner(
        cast("Any", transport),
        _connection(),
        SecretRegistry(),
    )

    result = asyncio.run(
        planner.plan(
            UserIntent(prompt="Find the critical checkout test space."),
            _decision(),
            source_context=_source_context(),
            snapshot_context=_snapshot_context(),
        )
    )

    assert len(result.draft.dimensions) == EXPECTED_DIMENSION_COUNT
    assert len(result.draft.sources) == EXPECTED_SOURCE_COUNT
    assert {item.kind for item in result.draft.sources} == {
        DecisionEvidenceKind.REQUIREMENTS,
        DecisionEvidenceKind.VERIFIED_UI,
    }
    assert all("content" not in item.model_dump() for item in result.draft.sources)
    serialized = result.draft.model_dump_json()
    assert "available inventory item" not in serialized
    assert '- button \\"Checkout\\"' not in serialized
    assert result.draft.repaired is False
    assert len(result.responses) == 1
    assert transport.calls[0]["response_model"] is GeneratedDecisionPlanPayload
    assert "available inventory item" in transport.calls[0]["user_prompt"]
    assert "checkout-button" in transport.calls[0]["user_prompt"]


def test_planner_repairs_one_incomplete_dimension_set() -> None:
    transport = _Transport(
        _payload(complete=False),
        _payload(),
    )
    planner = DecisionPlanner(
        cast("Any", transport),
        _connection(),
        SecretRegistry(),
    )

    result = asyncio.run(
        planner.plan(
            UserIntent(prompt="Find the critical checkout test space."),
            _decision(),
            snapshot_context=_snapshot_context(),
        )
    )

    assert result.draft.repaired is True
    assert len(result.responses) == EXPECTED_REPAIR_CALLS
    assert len(transport.calls) == EXPECTED_REPAIR_CALLS
    assert "missing dimensions" in transport.calls[1]["user_prompt"]
    assert SNAPSHOT_EVIDENCE_ID in transport.calls[1]["user_prompt"]


def test_planner_rejects_unknown_citations_after_one_repair() -> None:
    transport = _Transport(
        _payload(evidence_id=UNKNOWN_EVIDENCE_ID),
        _payload(evidence_id=UNKNOWN_EVIDENCE_ID),
    )
    planner = DecisionPlanner(
        cast("Any", transport),
        _connection(),
        SecretRegistry(),
    )

    with pytest.raises(DecisionPlanningError, match="valid decision plan"):
        asyncio.run(
            planner.plan(
                UserIntent(prompt="Find the critical checkout test space."),
                _decision(),
                snapshot_context=_snapshot_context(),
            )
        )

    assert len(transport.calls) == EXPECTED_REPAIR_CALLS


def test_planner_rejects_reflected_observed_secret_without_repair() -> None:
    sensitive_value = "private-planning-token"
    transport = _Transport(_payload(feature=sensitive_value))
    secrets = SecretRegistry(sensitive_keys=("agent_token",))
    secrets.observe_environment("agent_token", sensitive_value)
    planner = DecisionPlanner(
        cast("Any", transport),
        _connection(),
        secrets,
    )

    with pytest.raises(DecisionPlanningError, match="unsafe decision plan") as captured:
        asyncio.run(
            planner.plan(
                UserIntent(prompt="Find the critical checkout test space."),
                _decision(),
                snapshot_context=_snapshot_context(),
            )
        )

    assert len(transport.calls) == 1
    assert sensitive_value not in str(captured.value)
    assert sensitive_value not in repr(captured.value)


def test_planner_requires_evidence_before_provider_generation() -> None:
    transport = _Transport()
    planner = DecisionPlanner(
        cast("Any", transport),
        _connection(),
        SecretRegistry(),
    )

    with pytest.raises(DecisionPlanningError, match="needs verified UI evidence"):
        asyncio.run(
            planner.plan(
                UserIntent(prompt="Find the critical checkout test space."),
                _decision(),
            )
        )

    assert transport.calls == []
