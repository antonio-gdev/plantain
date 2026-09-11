"""Request and response validation across Swagger 2 and OpenAPI 3 operations."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from jsonschema import Draft4Validator, Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT4, DRAFT202012

from plantain.activities.api.client import ApiActivityError
from plantain.activities.api.openapi import OpenApiOperation, resolve_local_ref

_ROOT_URI = "urn:plantain:openapi"
_MEDIA_TYPE = re.compile(
    r"^(?:\*|[!#$%&'*+.^_`|~0-9a-z-]+)/"
    r"(?:\*|\*\+[!#$%&'*+.^_`|~0-9a-z-]+|[!#$%&'*+.^_`|~0-9a-z-]+)$"
)
MAX_COMPILED_VALIDATORS = 16
MAX_CACHEABLE_SCHEMA_BYTES = 1_048_576
MAX_MEDIA_TYPE_LENGTH = 255
MAX_SCHEMA_NORMALIZATION_DEPTH = 100
MAX_SCHEMA_NORMALIZATION_NODES = 100_000
SUPPORTED_PARAMETER_LOCATIONS = frozenset({"body", "header", "path", "query"})


class _Validator(Protocol):
    def iter_errors(self, instance: Any) -> Iterable[ValidationError]: ...


class SchemaConformanceError(ApiActivityError):
    """Raised when an instance does not conform without exposing rejected values."""


class _ValidationSession:
    """Bounded validator compilation scoped to one isolated validation call."""

    def __init__(self, document: dict[str, Any]) -> None:
        version = document.get("openapi")
        self._openapi_31 = isinstance(version, str) and version.startswith("3.1.")
        specification = DRAFT202012 if self._openapi_31 else DRAFT4
        self._registry = Registry().with_resource(
            _ROOT_URI,
            Resource.from_contents(
                document,
                default_specification=specification,
            ),
        )
        self._validators: OrderedDict[str, _Validator] = OrderedDict()

    def validator(self, schema: dict[str, Any]) -> _Validator:
        normalized = _normalize_schema(
            schema,
            openapi_31=self._openapi_31,
        )
        cache_key = _cacheable_schema_key(normalized)
        if cache_key is not None:
            cached = self._validators.pop(cache_key, None)
            if cached is not None:
                self._validators[cache_key] = cached
                return cached
        validator = self._compile(normalized)
        if cache_key is not None:
            if len(self._validators) >= MAX_COMPILED_VALIDATORS:
                self._validators.popitem(last=False)
            self._validators[cache_key] = validator
        return validator

    def _compile(self, schema: dict[str, Any]) -> _Validator:
        if self._openapi_31:
            Draft202012Validator.check_schema(schema)
            return Draft202012Validator(
                schema,
                registry=self._registry,
                format_checker=FormatChecker(),
            )
        Draft4Validator.check_schema(schema)
        return Draft4Validator(
            schema,
            registry=self._registry,
            format_checker=FormatChecker(),
        )


def _parameter_input(
    location: str,
    name: str,
    *,
    path_params: dict[str, Any],
    query: dict[str, Any],
    header_lookup: dict[str, str],
    body: Any,
) -> tuple[bool, Any, bool] | None:
    if location == "path":
        return name in path_params, path_params.get(name), False
    if location == "query":
        return name in query, query.get(name), False
    if location == "header":
        folded = name.casefold()
        return folded in header_lookup, header_lookup.get(folded), False
    if location == "body":
        return body is not None, body, True
    return None


def _parameter_types(
    document: dict[str, Any],
    parameter: dict[str, Any],
) -> set[str]:
    schema = resolve_local_ref(document, _parameter_schema(parameter))
    if not isinstance(schema, dict):
        return set()
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {item for item in declared if isinstance(item, str)}
    return set()


def _ensure_openapi3_parameter_serialization(
    parameter: dict[str, Any],
    location: str,
    *,
    is_array: bool,
    is_object: bool,
) -> None:
    expected_style = "form" if location == "query" else "simple"
    style = parameter.get("style", expected_style)
    explode = parameter.get("explode", location == "query")
    allow_reserved = parameter.get("allowReserved", False)
    supported = (
        style == expected_style
        and isinstance(explode, bool)
        and allow_reserved is False
        and not is_object
        and (not is_array or (location == "query" and explode))
    )
    if not supported:
        raise ApiActivityError(f"OpenAPI {location} parameter serialization is unsupported")


def _ensure_swagger_parameter_serialization(
    parameter: dict[str, Any],
    location: str,
    *,
    is_array: bool,
    is_object: bool,
) -> None:
    modern_fields = {"allowReserved", "explode", "style"}
    collection_format = parameter.get("collectionFormat")
    supported = (
        not modern_fields.intersection(parameter)
        and not is_object
        and (
            (is_array and location == "query" and collection_format == "multi")
            or (not is_array and collection_format is None)
        )
    )
    if not supported:
        raise ApiActivityError(f"OpenAPI {location} parameter serialization is unsupported")


def _ensure_parameter_serialization(
    document: dict[str, Any],
    parameter: dict[str, Any],
    location: str,
    *,
    present: bool,
    value: Any,
) -> None:
    if "content" in parameter:
        raise ApiActivityError(f"OpenAPI {location} parameter serialization is unsupported")
    if location == "body":
        if isinstance(document.get("openapi"), str):
            raise ApiActivityError("OpenAPI body parameter serialization is unsupported")
        return
    if present and (
        value is None or (isinstance(value, list) and any(item is None for item in value))
    ):
        raise ApiActivityError(f"OpenAPI {location} parameter serialization is unsupported")
    declared = _parameter_types(document, parameter)
    is_array = "array" in declared or isinstance(value, list)
    is_object = "object" in declared or isinstance(value, dict)
    if location == "header" and declared.difference({"null", "string"}):
        raise ApiActivityError("OpenAPI header parameter serialization is unsupported")
    gate = (
        _ensure_openapi3_parameter_serialization
        if isinstance(document.get("openapi"), str)
        else _ensure_swagger_parameter_serialization
    )
    gate(parameter, location, is_array=is_array, is_object=is_object)


def validate_operation_request(
    operation: OpenApiOperation,
    *,
    path_params: dict[str, Any],
    query: dict[str, Any],
    headers: dict[str, str],
    body: Any,
    content_type: str | None = None,
) -> None:
    """Validate supplied inputs without including their values in failures."""

    header_lookup = {key.casefold(): value for key, value in headers.items()}
    session = _ValidationSession(operation.document)
    body_validated = False
    for parameter in operation.parameters:
        location = str(parameter.get("in") or "")
        if location not in SUPPORTED_PARAMETER_LOCATIONS:
            rendered = location or "missing"
            raise ApiActivityError(f"OpenAPI parameter location '{rendered}' is unsupported")
        name = str(parameter.get("name") or "")
        required = bool(parameter.get("required")) or location == "path"
        supplied = _parameter_input(
            location,
            name,
            path_params=path_params,
            query=query,
            header_lookup=header_lookup,
            body=body,
        )
        if supplied is None:
            continue
        present, value, is_body = supplied
        _ensure_parameter_serialization(
            operation.document,
            parameter,
            location,
            present=present,
            value=value,
        )
        body_validated = body_validated or is_body
        if required and not present:
            raise ApiActivityError(
                f"Required OpenAPI {location} parameter '{name}' was not supplied"
            )
        if present:
            schema = parameter.get("schema") if location == "body" else _parameter_schema(parameter)
            if isinstance(schema, dict):
                validate_instance(
                    operation.document,
                    schema,
                    value,
                    label=f"{location} parameter '{name}'",
                    safe_details={"failure_stage": "request_schema_validation"},
                    _session=session,
                )

    request_body = operation.operation.get("requestBody")
    if request_body is not None:
        resolved = resolve_local_ref(operation.document, request_body)
        if not isinstance(resolved, dict):
            raise ApiActivityError("OpenAPI requestBody must resolve to an object")
        if bool(resolved.get("required")) and body is None:
            raise ApiActivityError("Required OpenAPI request body was not supplied")
        schema = (
            _content_schema(
                resolved.get("content"),
                content_type=content_type,
                label="OpenAPI request body",
            )
            if body is not None
            else None
        )
        if schema is not None:
            validate_instance(
                operation.document,
                schema,
                body,
                label="request body",
                safe_details={"failure_stage": "request_schema_validation"},
                _session=session,
            )
            body_validated = True

    if body is not None and body_validated:
        _validate_swagger_media_type(
            operation,
            "consumes",
            content_type,
            "OpenAPI request body",
        )
    if body is not None and not body_validated:
        # A body can still be valid for an underspecified operation, but strict
        # schema-based testing should surface the mismatch rather than silently pass it.
        raise ApiActivityError(
            "A request body was supplied but the operation defines no body schema"
        )


def validate_operation_response(
    operation: OpenApiOperation,
    *,
    status_code: int,
    body: Any,
    content_type: str | None = None,
) -> None:
    """Validate a decoded response against the exact/default/class response schema."""

    responses = operation.operation.get("responses")
    if not isinstance(responses, dict):
        raise ApiActivityError(
            "OpenAPI operation does not define responses",
            safe_details={
                "failure_stage": "response_contract_resolution",
                "http_status": status_code,
            },
        )
    raw = responses.get(str(status_code))
    if raw is None:
        raw = responses.get(f"{status_code // 100}XX") or responses.get(f"{status_code // 100}xx")
    if raw is None:
        raw = responses.get("default")
    if raw is None:
        raise ApiActivityError(
            f"OpenAPI operation defines no response contract for HTTP {status_code}",
            safe_details={
                "failure_stage": "response_contract_resolution",
                "http_status": status_code,
            },
        )
    response = resolve_local_ref(operation.document, raw)
    if not isinstance(response, dict):
        raise ApiActivityError(
            "OpenAPI response must resolve to an object",
            safe_details={
                "failure_stage": "response_contract_resolution",
                "http_status": status_code,
            },
        )
    declared_headers = response.get("headers")
    if declared_headers not in (None, {}):
        raise ApiActivityError(
            "OpenAPI response-header validation is unsupported",
            safe_details={
                "failure_stage": "response_contract_resolution",
                "http_status": status_code,
            },
        )
    schema = response.get("schema")
    if schema is None:
        schema = _content_schema(
            response.get("content"),
            content_type=content_type,
            label="OpenAPI response",
        )
    if schema is None:
        if body not in (None, ""):
            raise ApiActivityError(
                f"HTTP {status_code} returned a body but the OpenAPI response has no schema",
                safe_details={
                    "failure_stage": "response_schema_resolution",
                    "http_status": status_code,
                },
            )
        return
    _validate_swagger_media_type(
        operation,
        "produces",
        content_type,
        "OpenAPI response",
    )
    if not isinstance(schema, dict):
        raise ApiActivityError(
            "OpenAPI response schema must be an object",
            safe_details={
                "failure_stage": "response_schema_resolution",
                "http_status": status_code,
            },
        )
    validate_instance(
        operation.document,
        schema,
        body,
        label=f"HTTP {status_code} response",
        safe_details={
            "failure_stage": "response_schema_validation",
            "http_status": status_code,
        },
    )


def validate_instance(
    document: dict[str, Any],
    schema: dict[str, Any],
    instance: Any,
    *,
    label: str,
    safe_details: Mapping[str, str | int] | None = None,
    _session: _ValidationSession | None = None,
) -> None:
    """Validate with local-reference support and value-free diagnostics."""

    try:
        session = _session or _ValidationSession(document)
        validator = session.validator(schema)
        error = next(iter(validator.iter_errors(instance)), None)
    except SchemaError as exc:
        raise ApiActivityError(f"{label} uses an invalid JSON Schema") from exc
    if error is None:
        return
    instance_path = "/".join(str(item) for item in error.path) or "<root>"
    rule = str(error.validator or "unknown")
    raise SchemaConformanceError(
        f"{label} failed schema validation at '{instance_path}' (rule: {rule})",
        safe_details={
            **dict(safe_details or {}),
            "schema_path": instance_path,
            "schema_rule": rule,
        },
    )


def _cacheable_schema_key(schema: dict[str, Any]) -> str | None:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256()
    encoded_bytes = 0
    try:
        for fragment in encoder.iterencode(schema):
            encoded = fragment.encode("utf-8")
            encoded_bytes += len(encoded)
            if encoded_bytes > MAX_CACHEABLE_SCHEMA_BYTES:
                return None
            digest.update(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ApiActivityError("JSON Schema cannot be compiled safely") from exc
    return digest.hexdigest()


def _normalize_schema(
    value: Any,
    *,
    openapi_31: bool = False,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    if depth > MAX_SCHEMA_NORMALIZATION_DEPTH:
        raise ApiActivityError("JSON Schema exceeds the normalization depth limit")
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > MAX_SCHEMA_NORMALIZATION_NODES:
        raise ApiActivityError("JSON Schema exceeds the normalization node limit")
    if isinstance(value, list):
        return [
            _normalize_schema(
                item,
                openapi_31=openapi_31,
                depth=depth + 1,
                budget=budget,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    if any(not isinstance(key, str) for key in value):
        raise ApiActivityError("JSON Schema object keys must be strings")
    result = {
        key: _normalize_schema(
            item,
            openapi_31=openapi_31,
            depth=depth + 1,
            budget=budget,
        )
        for key, item in value.items()
    }
    reference = result.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        result["$ref"] = f"{_ROOT_URI}{reference}"
    return result if openapi_31 else _normalize_legacy_nullable(result)


def _normalize_legacy_nullable(result: dict[str, Any]) -> dict[str, Any]:
    if result.pop("nullable", False) is True:
        schema_type = result.get("type")
        if isinstance(schema_type, str):
            result["type"] = [schema_type, "null"]
        elif isinstance(schema_type, list):
            if "null" not in schema_type:
                result["type"] = [*schema_type, "null"]
        else:
            result = {"anyOf": [result, {"type": "null"}]}
    if result.pop("x-nullable", False) is True:
        schema_type = result.get("type")
        if isinstance(schema_type, str) and schema_type != "null":
            result["type"] = [schema_type, "null"]
    return result


def _parameter_schema(parameter: dict[str, Any]) -> dict[str, Any]:
    nested = parameter.get("schema")
    if isinstance(nested, dict):
        return nested
    return {
        key: parameter[key]
        for key in (
            "type",
            "format",
            "items",
            "enum",
            "default",
            "minimum",
            "maximum",
            "minLength",
            "maxLength",
            "pattern",
            "minItems",
            "maxItems",
            "uniqueItems",
        )
        if key in parameter
    }


def _validate_swagger_media_type(
    operation: OpenApiOperation,
    field: str,
    actual: str | None,
    label: str,
) -> None:
    if operation.document.get("swagger") != "2.0":
        return
    declared = operation.operation.get(field)
    if declared is None:
        declared = operation.document.get(field)
    if declared is None:
        return
    if (
        not isinstance(declared, list)
        or not declared
        or any(not isinstance(item, str) for item in declared)
    ):
        raise ApiActivityError(f"{label} media types are invalid")
    if actual is None:
        raise ApiActivityError(f"{label} requires an actual media type")
    normalized_actual = _normalize_media_type(actual)
    if not any(
        _media_match_score(_normalize_media_type(item), normalized_actual) >= 0 for item in declared
    ):
        raise ApiActivityError(f"{label} does not allow the actual media type")


def _content_schema(
    value: Any,
    *,
    content_type: str | None = None,
    label: str = "OpenAPI content",
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    entries = [
        (_normalize_media_type(media_type), media)
        for media_type, media in value.items()
        if isinstance(media_type, str)
    ]
    if len(entries) != len(value):
        raise ApiActivityError(f"{label} media types must be strings")
    selected = _select_media_entry(entries, content_type, label)
    if not isinstance(selected, dict):
        raise ApiActivityError(f"{label} media entry must be an object")
    schema = selected.get("schema")
    if schema is None:
        return None
    if not isinstance(schema, dict):
        raise ApiActivityError(f"{label} schema must be an object")
    return schema


def _select_media_entry(
    entries: list[tuple[str, Any]],
    content_type: str | None,
    label: str,
) -> Any:
    if content_type is None:
        if len(entries) != 1:
            raise ApiActivityError(f"{label} requires an actual media type")
        return entries[0][1]
    actual = _normalize_media_type(content_type)
    matches: list[tuple[int, Any]] = []
    for declared, media in entries:
        score = _media_match_score(declared, actual)
        if score >= 0:
            matches.append((score, media))
    if not matches:
        raise ApiActivityError(f"{label} does not define the actual media type")
    best_score = max(score for score, _media in matches)
    best = [media for score, media in matches if score == best_score]
    if len(best) != 1:
        raise ApiActivityError(f"{label} media type selection is ambiguous")
    return best[0]


def _normalize_media_type(value: str) -> str:
    rendered = value.split(";", 1)[0].strip().casefold()
    if len(rendered) > MAX_MEDIA_TYPE_LENGTH or _MEDIA_TYPE.fullmatch(rendered) is None:
        raise ApiActivityError("OpenAPI media type is invalid")
    return rendered


def _media_match_score(declared: str, actual: str) -> int:
    if declared == actual:
        return 3
    declared_type, declared_subtype = declared.split("/", 1)
    actual_type, actual_subtype = actual.split("/", 1)
    if declared_type not in {"*", actual_type}:
        return -1
    if declared_subtype == "*":
        return 1 if declared_type != "*" else 0
    if declared_subtype.startswith("*+") and actual_subtype.endswith(declared_subtype[1:]):
        return 2
    return -1
