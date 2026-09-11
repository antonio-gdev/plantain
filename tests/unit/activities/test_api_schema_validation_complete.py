"""Complete offline request and response schema-validation coverage."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.api.client import ApiActivityError
from plantain.activities.api.openapi import OpenApiOperation
from plantain.activities.api.schema_validation import (
    MAX_COMPILED_VALIDATORS,
    MAX_SCHEMA_NORMALIZATION_DEPTH,
    SchemaConformanceError,
    _content_schema,
    _normalize_schema,
    _parameter_schema,
    _ValidationSession,
    validate_instance,
    validate_operation_request,
    validate_operation_response,
)
from plantain.models.api import HttpMethod

HTTP_CREATED = 201
HTTP_NO_CONTENT = 204
HTTP_OK = 200


def _operation(
    *,
    document: dict[str, Any] | None = None,
    path_parameters: Any = None,
    operation_parameters: Any = None,
    request_body: Any = None,
    responses: Any = None,
) -> OpenApiOperation:
    operation: dict[str, Any] = {}
    if operation_parameters is not None:
        operation["parameters"] = operation_parameters
    if request_body is not None:
        operation["requestBody"] = request_body
    if responses is not None:
        operation["responses"] = responses
    path_item: dict[str, Any] = {}
    if path_parameters is not None:
        path_item["parameters"] = path_parameters
    return OpenApiOperation(
        method=HttpMethod.POST,
        path="/pets/{petId}",
        operation_id="createPet",
        document=document or {},
        path_item=path_item,
        operation=operation,
    )


def test_request_validates_path_query_and_case_insensitive_header_inputs() -> None:
    operation = _operation(
        operation_parameters=[
            {"name": "petId", "in": "path", "type": "integer"},
            {"name": "limit", "in": "query", "required": True, "type": "integer"},
            {"name": "X-Trace", "in": "header", "required": True, "type": "string"},
        ]
    )

    validate_operation_request(
        operation,
        path_params={"petId": 7},
        query={"limit": 3},
        headers={"x-trace": "trace-value"},
        body=None,
    )


@pytest.mark.parametrize("location", ["cookie", "formData", "unknown"])
def test_request_rejects_unsupported_parameter_locations(location: str) -> None:
    operation = _operation(
        operation_parameters=[
            {"name": "unsupported", "in": location, "type": "string"},
        ]
    )

    with pytest.raises(ApiActivityError, match=r"parameter location.*unsupported"):
        validate_operation_request(
            operation,
            path_params={},
            query={},
            headers={},
            body=None,
        )


@pytest.mark.parametrize(
    "parameter,value",
    [
        (
            {
                "name": "filter",
                "in": "query",
                "content": {"application/json": {"schema": {"type": "string"}}},
            },
            "value",
        ),
        (
            {
                "name": "filter",
                "in": "query",
                "style": "deepObject",
                "schema": {"type": "object"},
            },
            "value",
        ),
        (
            {
                "name": "tags",
                "in": "query",
                "style": "form",
                "explode": False,
                "schema": {"type": "array", "items": {"type": "string"}},
            },
            ["one", "two"],
        ),
    ],
)
def test_request_rejects_unsupported_parameter_serialization(
    parameter: dict[str, Any],
    value: Any,
) -> None:
    operation = _operation(
        document={"openapi": "3.0.3"},
        operation_parameters=[parameter],
    )

    with pytest.raises(ApiActivityError, match=r"serialization.*unsupported"):
        validate_operation_request(
            operation,
            path_params={},
            query={str(parameter["name"]): value},
            headers={},
            body=None,
        )


def test_request_accepts_repeated_query_array_serialization() -> None:
    operation = _operation(
        document={"openapi": "3.0.3"},
        operation_parameters=[
            {
                "name": "tags",
                "in": "query",
                "style": "form",
                "explode": True,
                "schema": {"type": "array", "items": {"type": "string"}},
            },
        ],
    )

    validate_operation_request(
        operation,
        path_params={},
        query={"tags": ["one", "two"]},
        headers={},
        body=None,
    )


def test_request_accepts_swagger_multi_query_array_serialization() -> None:
    operation = _operation(
        document={"swagger": "2.0"},
        operation_parameters=[
            {
                "name": "tags",
                "in": "query",
                "type": "array",
                "items": {"type": "string"},
                "collectionFormat": "multi",
            },
        ],
    )

    validate_operation_request(
        operation,
        path_params={},
        query={"tags": ["one", "two"]},
        headers={},
        body=None,
    )


def test_request_rejects_swagger_csv_query_array_serialization() -> None:
    operation = _operation(
        document={"swagger": "2.0"},
        operation_parameters=[
            {
                "name": "tags",
                "in": "query",
                "type": "array",
                "items": {"type": "string"},
                "collectionFormat": "csv",
            },
        ],
    )

    with pytest.raises(ApiActivityError, match="serialization is unsupported"):
        validate_operation_request(
            operation,
            path_params={},
            query={"tags": ["one", "two"]},
            headers={},
            body=None,
        )


def test_request_rejects_null_parameter_wire_values() -> None:
    operation = _operation(
        document={"openapi": "3.1.0"},
        operation_parameters=[
            {
                "name": "filter",
                "in": "query",
                "schema": {"type": ["string", "null"]},
            },
        ],
    )

    with pytest.raises(ApiActivityError, match="serialization is unsupported"):
        validate_operation_request(
            operation,
            path_params={},
            query={"filter": None},
            headers={},
            body=None,
        )


@pytest.mark.parametrize(
    "path_params, query, headers, expected_location",
    [
        ({}, {"limit": 3}, {"X-Trace": "present"}, "path"),
        ({"petId": 7}, {}, {"X-Trace": "present"}, "query"),
        ({"petId": 7}, {"limit": 3}, {}, "header"),
    ],
)
def test_request_rejects_missing_required_parameters(
    path_params: dict[str, Any],
    query: dict[str, Any],
    headers: dict[str, str],
    expected_location: str,
) -> None:
    operation = _operation(
        operation_parameters=[
            {"name": "petId", "in": "path", "type": "integer"},
            {"name": "limit", "in": "query", "required": True, "type": "integer"},
            {"name": "X-Trace", "in": "header", "required": True, "type": "string"},
        ]
    )

    with pytest.raises(ApiActivityError, match=f"Required OpenAPI {expected_location}"):
        validate_operation_request(
            operation,
            path_params=path_params,
            query=query,
            headers=headers,
            body=None,
        )


def test_request_parameter_schema_rejects_value_without_exposing_it() -> None:
    rejected = "synthetic-non-numeric-input"
    operation = _operation(
        operation_parameters=[{"name": "limit", "in": "query", "required": True, "type": "integer"}]
    )

    with pytest.raises(SchemaConformanceError) as captured:
        validate_operation_request(
            operation,
            path_params={},
            query={"limit": rejected},
            headers={},
            body=None,
        )

    assert rejected not in str(captured.value)
    assert captured.value.safe_details == {
        "failure_stage": "request_schema_validation",
        "schema_path": "<root>",
        "schema_rule": "type",
    }


def test_swagger_body_parameter_is_required_and_validated_once() -> None:
    operation = _operation(
        operation_parameters=[
            {
                "name": "body",
                "in": "body",
                "required": True,
                "schema": {"type": "object", "required": ["name"]},
            }
        ]
    )

    with pytest.raises(ApiActivityError, match="body parameter"):
        validate_operation_request(
            operation,
            path_params={},
            query={},
            headers={},
            body=None,
        )
    validate_operation_request(
        operation,
        path_params={},
        query={},
        headers={},
        body={"name": "Ada"},
    )


def test_swagger_body_requires_declared_actual_media_type() -> None:
    operation = _operation(
        document={"swagger": "2.0", "consumes": ["application/json"]},
        operation_parameters=[
            {
                "name": "body",
                "in": "body",
                "schema": {"type": "object"},
            },
        ],
    )
    inputs = {
        "path_params": {},
        "query": {},
        "headers": {},
        "body": {},
    }

    validate_operation_request(
        operation,
        content_type="application/json; charset=utf-8",
        **inputs,
    )
    with pytest.raises(ApiActivityError, match="does not allow the actual media type"):
        validate_operation_request(
            operation,
            content_type="text/plain",
            **inputs,
        )


def test_openapi_request_body_resolves_reference_and_fallback_media_type() -> None:
    document = {
        "components": {
            "requestBodies": {
                "Pet": {
                    "required": True,
                    "content": {
                        "application/xml": {"schema": {"type": "object", "required": ["name"]}}
                    },
                }
            }
        }
    }
    operation = _operation(
        document=document,
        request_body={"$ref": "#/components/requestBodies/Pet"},
    )

    with pytest.raises(ApiActivityError, match="Required OpenAPI request body"):
        validate_operation_request(
            operation,
            path_params={},
            query={},
            headers={},
            body=None,
        )
    validate_operation_request(
        operation,
        path_params={},
        query={},
        headers={},
        body={"name": "Ada"},
    )


def test_request_rejects_invalid_request_body_reference_shape() -> None:
    operation = _operation(
        document={"components": {"requestBodies": {"Invalid": []}}},
        request_body={"$ref": "#/components/requestBodies/Invalid"},
    )

    with pytest.raises(ApiActivityError, match="must resolve to an object"):
        validate_operation_request(
            operation,
            path_params={},
            query={},
            headers={},
            body=None,
        )


def test_request_rejects_body_when_operation_has_no_body_schema() -> None:
    operation = _operation(request_body={"required": False, "content": {"text/plain": {}}})

    with pytest.raises(ApiActivityError, match="defines no body schema"):
        validate_operation_request(
            operation,
            path_params={},
            query={},
            headers={},
            body={"name": "Ada"},
        )


def test_response_requires_responses_mapping() -> None:
    with pytest.raises(ApiActivityError, match="does not define responses") as captured:
        validate_operation_response(_operation(), status_code=200, body=None)

    assert captured.value.safe_details == {
        "failure_stage": "response_contract_resolution",
        "http_status": 200,
    }


@pytest.mark.parametrize(
    "responses, status",
    [
        ({"2XX": {"description": "ok", "schema": {"type": "object"}}}, HTTP_CREATED),
        ({"2xx": {"description": "ok", "schema": {"type": "object"}}}, HTTP_NO_CONTENT),
        ({"default": {"description": "ok", "schema": {"type": "object"}}}, 418),
    ],
)
def test_response_resolves_status_class_and_default_contracts(
    responses: dict[str, Any],
    status: int,
) -> None:
    validate_operation_response(_operation(responses=responses), status_code=status, body={})


def test_swagger_response_requires_declared_actual_media_type() -> None:
    operation = _operation(
        document={"swagger": "2.0", "produces": ["application/json"]},
        responses={
            "200": {
                "description": "ok",
                "schema": {"type": "object"},
            },
        },
    )

    validate_operation_response(
        operation,
        status_code=HTTP_OK,
        body={},
        content_type="application/json; charset=utf-8",
    )
    with pytest.raises(ApiActivityError, match="does not allow the actual media type"):
        validate_operation_response(
            operation,
            status_code=HTTP_OK,
            body={},
            content_type="text/plain",
        )


def test_response_requires_contract_for_actual_status() -> None:
    with pytest.raises(ApiActivityError, match="no response contract") as captured:
        validate_operation_response(
            _operation(responses={"200": {"description": "ok"}}),
            status_code=HTTP_CREATED,
            body=None,
        )

    assert captured.value.safe_details == {
        "failure_stage": "response_contract_resolution",
        "http_status": HTTP_CREATED,
    }


def test_response_reference_must_resolve_to_object() -> None:
    operation = _operation(
        document={"components": {"responses": {"Invalid": []}}},
        responses={"200": {"$ref": "#/components/responses/Invalid"}},
    )

    with pytest.raises(ApiActivityError, match="must resolve to an object") as captured:
        validate_operation_response(operation, status_code=200, body=None)
    assert captured.value.safe_details["http_status"] == HTTP_OK


@pytest.mark.parametrize("body", [None, ""])
def test_response_without_schema_allows_only_empty_body(body: Any) -> None:
    validate_operation_response(
        _operation(responses={"204": {"description": "empty"}}),
        status_code=HTTP_NO_CONTENT,
        body=body,
    )


def test_response_schema_must_be_object() -> None:
    with pytest.raises(ApiActivityError, match="schema must be an object") as captured:
        validate_operation_response(
            _operation(responses={"200": {"schema": []}}),
            status_code=200,
            body={},
        )
    assert captured.value.safe_details == {
        "failure_stage": "response_schema_resolution",
        "http_status": 200,
    }


def test_response_uses_actual_media_type_when_multiple_are_declared() -> None:
    responses = {
        "200": {
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "application/json": {"schema": {"type": "object"}},
            }
        }
    }
    operation = _operation(responses=responses)
    validate_operation_response(
        operation,
        status_code=200,
        body={},
        content_type="application/json; charset=utf-8",
    )
    with pytest.raises(ApiActivityError, match="requires an actual media type"):
        validate_operation_response(operation, status_code=200, body={})


def test_response_rejects_unsupported_header_contracts() -> None:
    operation = _operation(
        responses={
            "200": {
                "headers": {
                    "X-Rate-Limit": {
                        "schema": {"type": "integer"},
                    },
                },
                "content": {
                    "application/json": {
                        "schema": {"type": "object"},
                    },
                },
            },
        }
    )

    with pytest.raises(ApiActivityError, match="response-header validation is unsupported"):
        validate_operation_response(
            operation,
            status_code=200,
            body={},
            content_type="application/json",
        )


def test_validate_instance_checks_formats_and_never_echoes_value() -> None:
    rejected = "synthetic-invalid-address"

    with pytest.raises(SchemaConformanceError) as captured:
        validate_instance(
            {},
            {"type": "string", "format": "email"},
            rejected,
            label="email field",
            safe_details={"failure_stage": "request_schema_validation"},
        )

    assert rejected not in str(captured.value)
    assert captured.value.safe_details["schema_rule"] == "format"


def test_validate_instance_rejects_invalid_json_schema() -> None:
    with pytest.raises(ApiActivityError, match="uses an invalid JSON Schema"):
        validate_instance({}, {"type": 7}, None, label="invalid schema")


def test_validate_instance_consumes_only_the_first_error() -> None:
    class Validator:
        def iter_errors(self, _instance: Any) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(path=(), validator="type")
            raise AssertionError("validation errors were materialized")

    class Session:
        def validator(self, _schema: dict[str, Any]) -> Validator:
            return Validator()

    with pytest.raises(SchemaConformanceError):
        validate_instance(
            {},
            {},
            "rejected",
            label="bounded errors",
            _session=cast("Any", Session()),
        )


def test_nullable_is_expanded_only_for_legacy_openapi_dialects() -> None:
    schema = {"type": "string", "nullable": True}
    validate_instance(
        {"openapi": "3.0.3"},
        schema,
        None,
        label="legacy nullable",
    )
    with pytest.raises(SchemaConformanceError):
        validate_instance(
            {"openapi": "3.1.0"},
            schema,
            None,
            label="modern nullable",
        )


def test_openapi31_enforces_2020_12_const_keyword() -> None:
    schema = {"type": "integer", "const": 7}
    validate_instance(
        {"openapi": "3.0.3"},
        schema,
        8,
        label="legacy const",
    )
    with pytest.raises(SchemaConformanceError):
        validate_instance(
            {"openapi": "3.1.0"},
            schema,
            8,
            label="modern const",
        )


def test_validation_session_reuses_and_bounds_compiled_validators() -> None:
    session = _ValidationSession({"openapi": "3.1.0"})
    schema = {"type": "string", "minLength": 1}

    assert session.validator(schema) is session.validator(schema)
    for minimum in range(MAX_COMPILED_VALIDATORS + 2):
        session.validator({"type": "integer", "minimum": minimum})

    assert len(session._validators) == MAX_COMPILED_VALIDATORS


def test_normalize_schema_fails_explicitly_at_depth_limit() -> None:
    schema: dict[str, Any] = {}
    for _ in range(MAX_SCHEMA_NORMALIZATION_DEPTH + 1):
        schema = {"items": schema}

    with pytest.raises(ApiActivityError, match="normalization depth limit"):
        _normalize_schema(schema)


def test_normalize_schema_handles_nullable_extensions_and_metadata() -> None:
    normalized = _normalize_schema(
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "nullable": True,
                    "example": "Ada",
                    "xml": {"name": "pet"},
                    "discriminator": "kind",
                },
                "tags": {"type": ["array"], "nullable": True},
                "age": {"type": "integer", "x-nullable": True},
                "state": {"enum": ["ready"], "nullable": True},
                "reference": {"$ref": "#/definitions/Name"},
            },
        }
    )

    properties = normalized["properties"]
    assert properties["name"] == {
        "type": ["string", "null"],
        "example": "Ada",
        "xml": {"name": "pet"},
        "discriminator": "kind",
    }
    assert properties["tags"]["type"] == ["array", "null"]
    assert properties["age"]["type"] == ["integer", "null"]
    assert properties["state"] == {"anyOf": [{"enum": ["ready"]}, {"type": "null"}]}
    assert properties["reference"]["$ref"] == "urn:plantain:openapi#/definitions/Name"
    assert _normalize_schema([{"type": "string"}, 7]) == [{"type": "string"}, 7]


def test_normalize_schema_does_not_duplicate_null_or_expand_unsupported_x_nullable() -> None:
    assert _normalize_schema({"type": ["string", "null"], "nullable": True}) == {
        "type": ["string", "null"]
    }
    assert _normalize_schema({"type": ["string"], "x-nullable": True}) == {"type": ["string"]}
    assert _normalize_schema({"type": "null", "x-nullable": True}) == {"type": "null"}


def test_parameter_schema_prefers_nested_schema_and_copies_supported_constraints() -> None:
    nested = {"type": "string"}
    assert _parameter_schema({"schema": nested, "type": "integer"}) is nested
    assert _parameter_schema(
        {
            "name": "limit",
            "in": "query",
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "unknown": "ignored",
        }
    ) == {"type": "integer", "minimum": 1, "maximum": 10}


def test_content_schema_handles_invalid_and_fallback_media_entries() -> None:
    assert _content_schema(None) is None
    assert _content_schema({}) is None
    with pytest.raises(ApiActivityError, match="media entry must be an object"):
        _content_schema({"application/json": []})
    schema = {"type": "object"}
    content = {
        "application/json": {"schema": []},
        "application/xml": {"schema": schema},
    }
    with pytest.raises(ApiActivityError, match="requires an actual media type"):
        _content_schema(content)
    with pytest.raises(ApiActivityError, match="schema must be an object"):
        _content_schema(content, content_type="application/json")
    assert _content_schema(content, content_type="application/xml") is schema
