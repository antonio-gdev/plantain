"""Swagger 2 and OpenAPI 3 request/response schema validation tests."""

from __future__ import annotations

from typing import Any

import pytest

from plantain.activities.api.client import ApiActivityError
from plantain.activities.api.openapi import OpenApiOperation
from plantain.activities.api.schema_validation import (
    validate_operation_request,
    validate_operation_response,
)
from plantain.models.api import HttpMethod


def _swagger_operation() -> OpenApiOperation:
    document: dict[str, Any] = {
        "swagger": "2.0",
        "definitions": {
            "Pet": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    operation = {
        "parameters": [
            {
                "name": "body",
                "in": "body",
                "required": True,
                "schema": {"$ref": "#/definitions/Pet"},
            }
        ],
        "responses": {
            "200": {
                "description": "successful",
                "schema": {"$ref": "#/definitions/Pet"},
            }
        },
    }
    return OpenApiOperation(
        method=HttpMethod.POST,
        path="/pet",
        operation_id="addPet",
        document=document,
        path_item={},
        operation=operation,
    )


def test_swagger_local_references_validate_request_and_response() -> None:
    operation = _swagger_operation()

    validate_operation_request(
        operation,
        path_params={},
        query={},
        headers={},
        body={"name": "Ada"},
    )
    validate_operation_response(operation, status_code=200, body={"name": "Ada"})


def test_schema_failures_never_include_rejected_values() -> None:
    operation = _swagger_operation()
    rejected = "private-customer-value"

    with pytest.raises(ApiActivityError) as captured:
        validate_operation_request(
            operation,
            path_params={},
            query={},
            headers={},
            body={"name": {"unexpected": rejected}},
        )

    assert rejected not in str(captured.value)
    assert "rule: type" in str(captured.value)
    assert captured.value.safe_details == {
        "failure_stage": "request_schema_validation",
        "schema_path": "name",
        "schema_rule": "type",
    }


def test_response_schema_failure_includes_only_value_free_structured_details() -> None:
    operation = _swagger_operation()

    with pytest.raises(ApiActivityError) as captured:
        validate_operation_response(operation, status_code=200, body={})

    assert captured.value.safe_details == {
        "failure_stage": "response_schema_validation",
        "http_status": 200,
        "schema_path": "<root>",
        "schema_rule": "required",
    }


def test_response_body_is_rejected_when_contract_defines_no_schema() -> None:
    operation = _swagger_operation()
    operation.operation["responses"] = {"400": {"description": "invalid"}}

    with pytest.raises(ApiActivityError, match="returned a body"):
        validate_operation_response(
            operation,
            status_code=400,
            body={"message": "invalid"},
        )
