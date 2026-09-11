"""Dashboard scenario authoring stays typed, bounded, and locally validated."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.dashboard.agent.api_models import ApiWorkflowKind
from plantain.dashboard.agent.authoring import ScenarioAuthor, ScenarioAuthoringError
from plantain.dashboard.agent.connection import (
    AGENT_MODEL_ENV,
    AGENT_PROVIDER_ENV,
    AgentProvider,
    load_agent_connection,
)
from plantain.dashboard.agent.models import (
    AgentCapability,
    AgentResponseMetadata,
    GeneratedScenarioPayload,
    IntentAction,
    IntentDecision,
    UserIntent,
)
from plantain.dashboard.context_selection import (
    AgentContextExcerpt,
    AgentContextPacket,
)
from plantain.dashboard.context_sources import ContextSourceKind
from plantain.engine.loader import parse_scenario_text
from plantain.security.secrets import SecretRegistry

EXPECTED_REPAIR_CALLS = 2
VALID_API_SCENARIO = """\
scenario: Generated health check
steps:
  - sendRequest:
      id: health
      endpoint: https://api.example.test/health
      method: GET
      expectedStatus: 200
"""
INVALID_API_SCENARIO = """\
scenario: Missing activity identifier
steps:
  - sendRequest:
      endpoint: https://api.example.test/health
      method: GET
"""


class _Transport:
    def __init__(self, *payloads: GeneratedScenarioPayload) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        _connection: Any,
        **kwargs: Any,
    ) -> tuple[GeneratedScenarioPayload, AgentResponseMetadata]:
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


def _settings() -> Any:
    return cast(
        "Any",
        SimpleNamespace(
            yaml_max_bytes=2_000_000,
            yaml_max_nodes=50_000,
            yaml_max_depth=100,
        ),
    )


def _decision(capability: AgentCapability = AgentCapability.API_CONTRACT) -> IntentDecision:
    return IntentDecision(
        action=IntentAction.PLAN,
        capability=capability,
        summary="Create a contract-grounded API health check.",
        plan_steps=["Call the documented health operation.", "Validate its response."],
        api_workflow=(
            ApiWorkflowKind.REQUEST if capability is AgentCapability.API_CONTRACT else None
        ),
    )


def test_author_repairs_once_and_returns_canonical_validated_yaml() -> None:
    transport = _Transport(
        GeneratedScenarioPayload(scenario_yaml=INVALID_API_SCENARIO),
        GeneratedScenarioPayload(
            scenario_yaml=VALID_API_SCENARIO,
            directory="Payments / Regression",
        ),
    )
    source_context = AgentContextPacket(
        source_count=1,
        excerpts=[
            AgentContextExcerpt(
                source_kind=ContextSourceKind.REQUIREMENTS,
                source_label="health.md",
                relative_path="health.md",
                content="The health endpoint returns HTTP 200.",
            )
        ],
    )
    author = ScenarioAuthor(
        cast("Any", transport),
        _connection(),
        _settings(),
        SecretRegistry(),
    )

    result = asyncio.run(
        author.author(
            UserIntent(prompt="Create a health endpoint test."),
            _decision(),
            source_context=source_context,
        )
    )

    parsed = parse_scenario_text(result.draft.yaml_text, _settings())
    assert result.draft.scenario == "Generated health check"
    assert result.draft.step_count == 1
    assert result.draft.activities == ["sendRequest"]
    assert result.draft.suggested_directory == "payments/regression"
    assert result.draft.repaired is True
    assert parsed.steps[0].params["id"] == "health"
    assert len(result.responses) == EXPECTED_REPAIR_CALLS
    assert len(transport.calls) == EXPECTED_REPAIR_CALLS
    assert transport.calls[0]["response_model"] is GeneratedScenarioPayload
    assert "sendRequest" in transport.calls[0]["system_prompt"]
    assert "capturePageSnapshot" not in transport.calls[0]["system_prompt"]
    assert "relative folder beneath scenarios" in transport.calls[0]["system_prompt"]
    assert "health endpoint returns HTTP 200" in transport.calls[0]["user_prompt"]
    assert "Value-free validation feedback" in transport.calls[1]["user_prompt"]
    assert "```" not in result.draft.yaml_text


def test_author_rejects_reflected_observed_credential_without_repair() -> None:
    credential = "provider-credential-value"
    unsafe = VALID_API_SCENARIO.replace(
        "/health",
        f"/health?token={credential}",
    )
    transport = _Transport(GeneratedScenarioPayload(scenario_yaml=unsafe))
    secrets = SecretRegistry(sensitive_keys=("provider_credential",))
    secrets.observe_environment("provider_credential", credential)
    author = ScenarioAuthor(
        cast("Any", transport),
        _connection(),
        _settings(),
        secrets,
    )

    with pytest.raises(ScenarioAuthoringError, match="unsafe scenario draft") as captured:
        asyncio.run(
            author.author(
                UserIntent(prompt="Create a health endpoint test."),
                _decision(),
            )
        )

    assert len(transport.calls) == 1
    assert credential not in str(captured.value)
    assert credential not in repr(captured.value)


def test_author_rejects_activity_outside_routed_capability_after_one_repair() -> None:
    unsupported = """\
scenario: Unsupported workflow
steps:
  - unknownActivity:
      id: unknown
"""
    transport = _Transport(
        GeneratedScenarioPayload(scenario_yaml=unsupported),
        GeneratedScenarioPayload(scenario_yaml=unsupported),
    )
    author = ScenarioAuthor(
        cast("Any", transport),
        _connection(),
        _settings(),
        SecretRegistry(),
    )

    with pytest.raises(ScenarioAuthoringError, match="valid scenario draft"):
        asyncio.run(
            author.author(
                UserIntent(prompt="Create an API test."),
                _decision(),
            )
        )

    assert len(transport.calls) == EXPECTED_REPAIR_CALLS


def test_author_repairs_unsafe_suggested_directory_once() -> None:
    transport = _Transport(
        GeneratedScenarioPayload(
            scenario_yaml=VALID_API_SCENARIO,
            directory="../outside",
        ),
        GeneratedScenarioPayload(
            scenario_yaml=VALID_API_SCENARIO,
            directory="API Team",
        ),
    )
    author = ScenarioAuthor(
        cast("Any", transport),
        _connection(),
        _settings(),
        SecretRegistry(),
    )

    result = asyncio.run(
        author.author(
            UserIntent(prompt="Save this in the API Team folder."),
            _decision(),
        )
    )

    assert result.draft.suggested_directory == "api-team"
    assert result.draft.repaired is True
    assert len(transport.calls) == EXPECTED_REPAIR_CALLS


def test_author_rejects_workflow_that_does_not_create_scenario_yaml() -> None:
    transport = _Transport()
    author = ScenarioAuthor(
        cast("Any", transport),
        _connection(),
        _settings(),
        SecretRegistry(),
    )

    with pytest.raises(ScenarioAuthoringError, match="does not produce scenario YAML"):
        asyncio.run(
            author.author(
                UserIntent(prompt="Explain my latest run."),
                _decision(AgentCapability.WORKSPACE_QUESTION),
            )
        )

    assert transport.calls == []
