"""YAML-facing HTTP and Swagger/OpenAPI activity handlers."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar
from urllib.parse import quote

from plantain.activities.api.client import (
    MAX_REQUEST_BODY_BYTES,
    ApiActivityError,
    assert_expected_status,
    build_request_url,
    request_content_type,
)
from plantain.activities.api.openapi import LoadedOpenApi, OpenApiOperation
from plantain.activities.api.schema_validation import (
    SchemaConformanceError,
    validate_operation_request,
    validate_operation_response,
)
from plantain.activities.api.validation_worker import run_isolated_api_validation
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runtime import RunContext
from plantain.errors import AtomicPersistenceError
from plantain.models.api import (
    ApiResponseResult,
    CallSchemaParams,
    HttpMethod,
    LoadApiSchemaParams,
    LoadApiSchemaResult,
    SendRequestParams,
    ValidateSchemaParams,
    ValidateSchemaResult,
)
from plantain.observability import get_logger
from plantain.persistence import open_binary_read_no_follow
from plantain.security.redaction import REDACTED, redact_url

_MAX_LOGGED_RESPONSE_BYTES = 8 * 1_024
_JWT_LIKE_SCALAR = re.compile(r"^[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}$")
_LONG_OPAQUE_SCALAR = re.compile(r"^\S{64,}$")
logger = get_logger("activities.api")
_P = ParamSpec("_P")
_T = TypeVar("_T")


def _record_preflight_failures(
    *,
    activity: str,
    phase: str,
) -> Callable[
    [Callable[Concatenate[RunContext, _P], Awaitable[_T]]],
    Callable[Concatenate[RunContext, _P], Awaitable[_T]],
]:
    def decorate(
        handler: Callable[Concatenate[RunContext, _P], Awaitable[_T]],
    ) -> Callable[Concatenate[RunContext, _P], Awaitable[_T]]:
        @wraps(handler)
        async def wrapped(
            context: RunContext,
            /,
            *args: _P.args,
            **kwargs: _P.kwargs,
        ) -> _T:
            operation_count = len(context.operations)
            started = time.monotonic()
            try:
                return await handler(context, *args, **kwargs)
            except Exception as exc:
                if len(context.operations) == operation_count:
                    _record_api_preflight_failure(context, activity, phase, started, exc)
                raise

        return wrapped

    return decorate


@_record_preflight_failures(activity="sendRequest", phase="request_preflight")
async def send_request(
    context: RunContext,
    params: SendRequestParams,
) -> ApiResponseResult:
    """Execute one bounded HTTP request and retain its decoded response for chaining."""

    _enforce_method_policy(context, params.method)
    api = await context.services.api()
    body = _request_body(context, params.file, params.body)
    url = build_request_url(params.endpoint, params.base_url)

    async def execute() -> ApiResponseResult:
        response = await api.request(
            method=params.method,
            url=url,
            headers=params.headers,
            query=params.query,
            body=body,
            body_format=params.body_format,
            timeout_seconds=params.timeout_seconds,
        )
        decoded = response.decoded_body()
        _log_api_response(
            context,
            activity="sendRequest",
            step_id=params.id,
            response=response,
            decoded=decoded,
            include_body=params.log_response_body,
        )
        assert_expected_status(response.status_code, params.expected_status)
        _assert_expected_message(decoded, params.expected_message, response.status_code)
        return _response_result(response, request_body=body, decoded=decoded)

    return await _run_api_operation(
        context,
        phase="request",
        operation_type=params.method.value,
        target=redact_url(url),
        operation_input=_request_evidence(params.headers, params.query, body),
        operation_expected=_request_expectation(params),
        execute=execute,
        actual=_response_evidence,
    )


@_record_preflight_failures(activity="loadApiSchema", phase="schema_download_preflight")
async def load_api_schema(
    context: RunContext,
    params: LoadApiSchemaParams,
) -> LoadApiSchemaResult:
    """Download only when schema work is requested, then content-address the result."""

    async def execute() -> LoadApiSchemaResult:
        loaded = await (await context.services.openapi_store()).load_url(
            params.schema_url,
            headers=params.headers,
        )
        relative = loaded.cache_file.relative_to(context.settings.project_root).as_posix()
        context.add_artifact(
            kind="openapi-schema",
            path=relative,
            description="Validated content-addressed Swagger/OpenAPI document",
        )
        return LoadApiSchemaResult(
            schema_id=loaded.schema_id,
            schema_version=loaded.schema_version,
            source_url=loaded.source_url,
            base_url=redact_url(loaded.base_url),
            cache_file=relative,
            operations=loaded.summaries(),
        )

    return await _run_api_operation(
        context,
        phase="schema_download",
        operation_type="download",
        target=redact_url(params.schema_url),
        operation_input={"headers": params.headers},
        operation_expected={"contract": "Swagger 2 or OpenAPI 3"},
        execute=execute,
        actual=_loaded_schema_evidence,
    )


@_record_preflight_failures(activity="callSchema", phase="schema_request_preflight")
async def call_schema(
    context: RunContext,
    params: CallSchemaParams,
) -> ApiResponseResult:
    """Resolve, optionally validate, invoke, and verify one API-schema operation."""

    loaded = await _load_selected_schema(
        context,
        params.schema_url,
        params.schema_id,
        params.schema_headers,
    )
    operation = loaded.operation(
        operation_id=params.operation_id,
        path=params.path,
        method=params.method,
    )
    _enforce_method_policy(context, operation.method)
    body = _request_body(context, params.file, params.body)
    if params.validate_request:
        await run_isolated_api_validation(
            context.services.admission,
            context.settings,
            validate_operation_request,
            operation,
            path_params=params.path_params,
            query=params.query,
            headers=params.headers,
            body=body,
            content_type=request_content_type(
                body,
                params.body_format,
                params.headers,
            ),
        )
    operation_path = _render_path(operation, params.path_params)
    url = build_request_url(operation_path, loaded.base_url)
    api = await context.services.api()

    async def execute() -> ApiResponseResult:
        response = await api.request(
            method=operation.method,
            url=url,
            headers=params.headers,
            query=params.query,
            body=body,
            body_format=params.body_format,
            timeout_seconds=params.timeout_seconds,
        )
        decoded = response.decoded_body()
        _log_api_response(
            context,
            activity="callSchema",
            step_id=params.id,
            response=response,
            decoded=decoded,
            include_body=params.log_response_body,
        )
        assert_expected_status(response.status_code, params.expected_status)
        _assert_expected_message(decoded, params.expected_message, response.status_code)
        if params.validate_response:
            await run_isolated_api_validation(
                context.services.admission,
                context.settings,
                validate_operation_response,
                operation,
                status_code=response.status_code,
                body=decoded,
                content_type=response.headers.get("content-type"),
            )
        return _response_result(response, request_body=body, decoded=decoded)

    evidence = _request_evidence(params.headers, params.query, body)
    evidence["pathParams"] = params.path_params
    evidence["operationId"] = operation.operation_id
    return await _run_api_operation(
        context,
        phase="schema_request",
        operation_type=operation.method.value,
        target=redact_url(url),
        operation_input=evidence,
        operation_expected=_request_expectation(params),
        execute=execute,
        actual=_response_evidence,
    )


def _log_api_response(
    context: RunContext,
    *,
    activity: str,
    step_id: str,
    response: Any,
    decoded: Any,
    include_body: bool,
) -> None:
    """Log bounded response diagnostics without exposing context-held raw data."""

    context.secrets.observe(decoded)
    extra: dict[str, Any] = {
        "scenario": context.scenario.scenario,
        "activity": activity,
        "step_id": step_id,
        "http_status": response.status_code,
        "http_method": response.method.value,
        "response_bytes": len(response.content),
        "response_body_truncated": False,
    }
    if include_body:
        redacted = context.secrets.redact_log(decoded)
        safe_body = _mask_opaque_scalars(redacted)
        bounded_body, truncated = _bounded_log_body(safe_body)
        extra["response_body"] = bounded_body
        extra["response_body_truncated"] = truncated
    logger.info("HTTP response received", extra=extra)


async def _run_api_operation(
    context: RunContext,
    *,
    phase: str,
    operation_type: str,
    target: str,
    operation_input: object | None,
    operation_expected: object | None,
    execute: Callable[[], Awaitable[_T]],
    actual: Callable[[_T], object | None],
) -> _T:
    safe_input = _bounded_operation_value(context, operation_input)
    safe_expected = _bounded_operation_value(context, operation_expected)
    extra = {
        "scenario": context.scenario.scenario,
        "activity": context.current_activity or "api",
        "step_id": context.current_step_id or "api",
        "operation_type": operation_type,
        "operation_target": target,
        "operation_input": safe_input,
        "operation_expected": safe_expected,
    }
    logger.info("API operation started", extra={**extra, "status": "running"})
    started = time.monotonic()
    try:
        result = await execute()
    except Exception as exc:
        duration_ms = max(0, round((time.monotonic() - started) * 1_000))
        safe_actual = _bounded_operation_value(context, _failure_evidence(exc))
        context.add_operation(
            domain="api",
            phase=phase,
            operation_type=operation_type,
            target=target,
            status="failed",
            duration_ms=duration_ms,
            operation_input=safe_input,
            operation_expected=safe_expected,
            operation_actual=safe_actual,
            error_type=type(exc).__name__,
        )
        logger.error(  # noqa: TRY400 - tracebacks may expose request data.
            "API operation failed",
            extra={
                **extra,
                "status": "failed",
                "duration_ms": duration_ms,
                "operation_actual": safe_actual,
                "operation_error_type": type(exc).__name__,
            },
        )
        raise
    duration_ms = max(0, round((time.monotonic() - started) * 1_000))
    safe_actual = _bounded_operation_value(context, actual(result))
    context.add_operation(
        domain="api",
        phase=phase,
        operation_type=operation_type,
        target=target,
        status="passed",
        duration_ms=duration_ms,
        operation_input=safe_input,
        operation_expected=safe_expected,
        operation_actual=safe_actual,
    )
    logger.info(
        "API operation passed",
        extra={
            **extra,
            "status": "passed",
            "duration_ms": duration_ms,
            "operation_actual": safe_actual,
        },
    )
    return result


def _request_evidence(
    headers: Mapping[str, Any],
    query: Mapping[str, Any],
    body: Any,
) -> dict[str, Any]:
    return {"headers": dict(headers), "query": dict(query), "body": body}


def _request_expectation(
    params: SendRequestParams | CallSchemaParams,
) -> dict[str, Any]:
    expectation: dict[str, Any] = {"statusCode": params.expected_status}
    if params.expected_message is not None:
        expectation["messageContains"] = params.expected_message
    return expectation


def _response_evidence(result: ApiResponseResult) -> dict[str, Any]:
    return {
        "httpStatus": result.status_code,
        "responseBytes": result.response_bytes,
        "elapsedMs": result.elapsed_ms,
    }


def _loaded_schema_evidence(result: LoadApiSchemaResult) -> dict[str, Any]:
    return {
        "schemaId": result.schema_id,
        "schemaVersion": result.schema_version,
        "operationCount": len(result.operations),
    }


def _validated_schema_evidence(result: ValidateSchemaResult) -> dict[str, Any]:
    return {
        "httpStatus": result.status_code,
        "schemaValid": result.schema_valid,
        "schemaPath": result.schema_path,
        "schemaRule": result.schema_rule,
    }


def _bounded_operation_value(context: RunContext, value: Any) -> Any:
    if value is None:
        return None
    safe = _mask_opaque_scalars(context.secrets.redact_log(value))
    try:
        bounded, truncated = _bounded_log_body(safe)
    except (TypeError, ValueError):
        return "<unavailable>"
    if truncated:
        return {"preview": bounded, "truncated": True}
    return bounded


def _failure_evidence(error: Exception) -> dict[str, Any] | None:
    details = getattr(error, "safe_details", None)
    if not isinstance(details, Mapping):
        return None
    allowed = {"failure_stage", "http_status", "schema_path", "schema_rule"}
    selected = {str(key): value for key, value in details.items() if key in allowed}
    return selected or None


def _record_api_preflight_failure(
    context: RunContext,
    activity: str,
    phase: str,
    started: float,
    error: Exception,
) -> None:
    details = _failure_evidence(error) or {"failure_stage": phase}
    context.add_operation(
        domain="api",
        phase=phase,
        operation_type="prepare",
        target=activity,
        status="failed",
        duration_ms=max(0, round((time.monotonic() - started) * 1_000)),
        operation_input=None,
        operation_expected=None,
        operation_actual=_bounded_operation_value(context, details),
        error_type=type(error).__name__,
    )


def _assert_expected_message(body: Any, expected: str | None, status_code: int) -> None:
    if expected is None:
        return
    rendered = (
        body
        if isinstance(body, str)
        else json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )
    if expected.strip().casefold() not in rendered.casefold():
        raise ApiActivityError(
            "HTTP response did not contain the expected message",
            safe_details={
                "failure_stage": "response_message_validation",
                "http_status": status_code,
            },
        )


def _mask_opaque_scalars(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _mask_opaque_scalars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_opaque_scalars(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_opaque_scalars(item) for item in value)
    if isinstance(value, str):
        stripped = value.strip()
        if _JWT_LIKE_SCALAR.fullmatch(stripped) or _LONG_OPAQUE_SCALAR.fullmatch(stripped):
            return REDACTED
    return value


def _bounded_log_body(value: Any) -> tuple[Any, bool]:
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    encoded = rendered.encode("utf-8")
    if len(encoded) <= _MAX_LOGGED_RESPONSE_BYTES:
        return value, False
    prefix = encoded[:_MAX_LOGGED_RESPONSE_BYTES].decode("utf-8", errors="ignore")
    return f"{prefix}…", True


@_record_preflight_failures(activity="validateSchema", phase="schema_validation_preflight")
async def validate_schema(
    context: RunContext,
    params: ValidateSchemaParams,
) -> ValidateSchemaResult:
    """Validate a response already held in scenario context without another request."""

    loaded = await _load_selected_schema(
        context,
        params.schema_url,
        params.schema_id,
        params.schema_headers,
    )
    operation = loaded.operation(
        operation_id=params.operation_id,
        path=params.path,
        method=params.method,
    )

    async def execute() -> ValidateSchemaResult:
        schema_path: str | None = None
        schema_rule: str | None = None
        try:
            await run_isolated_api_validation(
                context.services.admission,
                context.settings,
                validate_operation_response,
                operation,
                status_code=params.status_code,
                body=params.body,
                content_type=params.content_type,
            )
            schema_valid = True
        except SchemaConformanceError as exc:
            if params.expected_valid:
                raise
            schema_valid = False
            schema_path = str(exc.safe_details.get("schema_path") or "<root>")
            schema_rule = str(exc.safe_details.get("schema_rule") or "unknown")
        if schema_valid is not params.expected_valid:
            raise ApiActivityError(
                f"HTTP {params.status_code} response unexpectedly satisfied its schema",
                safe_details={
                    "failure_stage": "response_schema_expectation",
                    "http_status": params.status_code,
                },
            )
        return ValidateSchemaResult(
            schema_id=loaded.schema_id,
            operation_id=operation.operation_id,
            method=operation.method,
            path=operation.path,
            status_code=params.status_code,
            expected_valid=params.expected_valid,
            schema_valid=schema_valid,
            schema_path=schema_path,
            schema_rule=schema_rule,
        )

    return await _run_api_operation(
        context,
        phase="schema_validation",
        operation_type="validate",
        target=f"{operation.method.value} {operation.path}",
        operation_input={"statusCode": params.status_code, "body": params.body},
        operation_expected={"schemaValid": params.expected_valid},
        execute=execute,
        actual=_validated_schema_evidence,
    )


def register_api_activities(registry: ActivityRegistry) -> None:
    """Register stable generic API activities."""

    registry.register(
        "sendRequest",
        SendRequestParams,
        send_request,
        description="Execute a bounded HTTP request with reusable scenario context output",
    )
    registry.register(
        "loadApiSchema",
        LoadApiSchemaParams,
        load_api_schema,
        description="Download, validate, and content-address a Swagger 2 or OpenAPI 3 document",
    )
    registry.register(
        "callSchema",
        CallSchemaParams,
        call_schema,
        description="Invoke and validate an operation selected from Swagger/OpenAPI metadata",
    )
    registry.register(
        "validateSchema",
        ValidateSchemaParams,
        validate_schema,
        description="Validate an existing response against a Swagger/OpenAPI operation",
    )


async def _load_selected_schema(
    context: RunContext,
    schema_url: str | None,
    schema_id: str | None,
    schema_headers: dict[str, str],
) -> LoadedOpenApi:
    store = await context.services.openapi_store()
    if schema_url is not None:
        return await store.load_url(schema_url, headers=schema_headers)
    return await store.load_id(schema_id or "")


def _request_body(context: RunContext, filename: str | None, inline: Any) -> Any:
    if filename is None:
        return inline
    root = context.settings.api_data_dir.resolve()
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ApiActivityError("API data file must stay inside the configured api-data directory")
    candidate = root / relative
    if relative.suffix.casefold() != ".json":
        raise ApiActivityError("External API data files must use the .json extension")
    try:
        with open_binary_read_no_follow(candidate) as handle:
            raw = handle.read(MAX_REQUEST_BODY_BYTES + 1)
    except FileNotFoundError:
        raise ApiActivityError(f"API data file does not exist: {relative.name}") from None
    except (OSError, AtomicPersistenceError) as exc:
        raise ApiActivityError("API data file must be readable UTF-8") from exc
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        raise ApiActivityError("API data file exceeds the configured byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiActivityError("API data file must be readable UTF-8") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ApiActivityError("API data file contains invalid JSON") from exc
    return value


def _render_path(operation: OpenApiOperation, supplied: dict[str, Any]) -> str:
    rendered = operation.path
    required = {
        str(parameter.get("name"))
        for parameter in operation.parameters
        if parameter.get("in") == "path"
    }
    missing = sorted(name for name in required if name not in supplied)
    if missing:
        raise ApiActivityError(f"Missing OpenAPI path parameters: {', '.join(missing)}")
    extras = sorted(set(supplied) - required)
    if extras:
        raise ApiActivityError(f"Unknown OpenAPI path parameters: {', '.join(extras)}")
    for name in required:
        rendered = rendered.replace("{" + name + "}", quote(str(supplied[name]), safe=""))
    if "{" in rendered or "}" in rendered:
        raise ApiActivityError("OpenAPI path contains an unresolved template variable")
    return rendered


def _response_result(
    response: Any,
    *,
    request_body: Any,
    decoded: Any = ...,
) -> ApiResponseResult:
    body = response.decoded_body() if decoded is ... else decoded
    return ApiResponseResult(
        request_url=response.request_url,
        method=response.method,
        request_body=request_body,
        status_code=response.status_code,
        headers=response.headers,
        content_type=response.headers.get("content-type"),
        response_body=body,
        response_bytes=len(response.content),
        elapsed_ms=response.elapsed_ms,
    )


def _enforce_method_policy(context: RunContext, method: HttpMethod) -> None:
    allowed = getattr(context.settings, "api_allowed_methods", ())
    if allowed and method.value not in allowed:
        raise ApiActivityError("HTTP method is not permitted by PLANTAIN_API_ALLOWED_METHODS")


__all__ = ("register_api_activities",)

# Agent-facing API access is intentionally limited to the four registered handlers above.
