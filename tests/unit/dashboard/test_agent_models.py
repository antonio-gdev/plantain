"""Strict observable contracts for automatic intent routing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plantain.dashboard.agent.api_models import (
    ApiSchemaHeaderReference,
    ApiWorkflowKind,
)
from plantain.dashboard.agent.models import (
    AgentCapability,
    IntentAction,
    IntentDecision,
    ScenarioOperation,
    UserIntent,
)


def test_plan_requires_internal_capability_and_observable_steps() -> None:
    decision = IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.UI_DISCOVERY,
        summary="Inspect the live page before generating resilient automation.",
        plan_steps=[
            "Capture current semantic page evidence.",
            "Ground the requested flow in verified locators.",
        ],
    )

    assert decision.capability is AgentCapability.UI_DISCOVERY
    assert "reasoning" not in decision.model_dump()

    with pytest.raises(ValidationError, match="requires at least one observable step"):
        IntentDecision(
            action=IntentAction.PLAN,
            capability=AgentCapability.API_CONTRACT,
            summary="Validate an API contract.",
        )


def test_clarification_requires_question_and_rejects_premature_plan() -> None:
    decision = IntentDecision(
        action=IntentAction.CLARIFY,
        summary="The target application is not yet identified.",
        question="What URL should Plantain inspect?",
        required_inputs=["Application URL"],
    )
    assert decision.question == "What URL should Plantain inspect?"

    with pytest.raises(ValidationError, match="cannot contain plan steps"):
        IntentDecision(
            action=IntentAction.CLARIFY,
            summary="More information is required.",
            question="Which environment should be tested?",
            plan_steps=["Run the test."],
        )


def test_scenario_execution_requires_scoped_operation_fields() -> None:
    decision = IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.SCENARIO_EXECUTION,
        summary="Run the checkout scenario.",
        plan_steps=["Match the current local scenario.", "Run it after review."],
        scenario_operation=ScenarioOperation.RUN,
        scenario_query="checkout",
    )

    assert decision.scenario_operation is ScenarioOperation.RUN
    assert decision.scenario_query == "checkout"

    with pytest.raises(ValidationError, match="requires an operation"):
        IntentDecision(
            action=IntentAction.PLAN,
            capability=AgentCapability.SCENARIO_EXECUTION,
            summary="Run a local scenario.",
            plan_steps=["Match the scenario."],
        )

    with pytest.raises(ValidationError, match="Only scenario execution"):
        IntentDecision(
            action=IntentAction.PLAN,
            capability=AgentCapability.API_CONTRACT,
            summary="Validate an API contract.",
            plan_steps=["Load the contract."],
            scenario_operation=ScenarioOperation.RUN,
            scenario_query="checkout",
        )


def test_user_intent_rejects_empty_input() -> None:
    with pytest.raises(ValidationError):
        UserIntent(prompt="")


def test_api_workflow_distinguishes_generic_requests_from_contract_work() -> None:
    request = IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.API_CONTRACT,
        summary="Create a direct API request.",
        plan_steps=["Send the requested API call."],
        api_workflow=ApiWorkflowKind.REQUEST,
    )
    contract = IntentDecision(
        action=IntentAction.PLAN,
        capability=AgentCapability.API_CONTRACT,
        summary="Create a schema-grounded API test.",
        plan_steps=["Inspect the supplied contract."],
        api_workflow=ApiWorkflowKind.CONTRACT,
        api_schema_reference="env:PETSTORE_SCHEMA_URL",
        api_schema_headers=[
            ApiSchemaHeaderReference(
                name="Authorization",
                value="env:SCHEMA_TOKEN",
            )
        ],
        api_operation_query="find available pets",
    )

    assert request.api_schema_reference is None
    assert contract.api_operation_query == "find available pets"

    with pytest.raises(ValidationError, match="requires an operation query"):
        IntentDecision(
            action=IntentAction.PLAN,
            capability=AgentCapability.API_CONTRACT,
            summary="Inspect a contract.",
            plan_steps=["Inspect it."],
            api_workflow=ApiWorkflowKind.CONTRACT,
        )
    with pytest.raises(ValidationError, match="cannot select a schema"):
        IntentDecision(
            action=IntentAction.PLAN,
            capability=AgentCapability.API_CONTRACT,
            summary="Send a direct request.",
            plan_steps=["Send it."],
            api_workflow=ApiWorkflowKind.REQUEST,
            api_schema_reference="https://api.example.test/openapi.json",
        )
    with pytest.raises(ValidationError, match="Schema headers require"):
        IntentDecision(
            action=IntentAction.PLAN,
            capability=AgentCapability.API_CONTRACT,
            summary="Inspect a protected contract.",
            plan_steps=["Inspect it."],
            api_workflow=ApiWorkflowKind.CONTRACT,
            api_schema_headers=[
                ApiSchemaHeaderReference(
                    name="Authorization",
                    value="env:SCHEMA_TOKEN",
                )
            ],
            api_operation_query="pets",
        )
