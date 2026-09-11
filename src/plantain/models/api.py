"""Strict YAML contracts for generic HTTP and OpenAPI activities."""

from __future__ import annotations

import math
import re
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from plantain.models.common import StrictModel

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_REQUEST_MANAGED_HEADERS = frozenset({"content-length", "host", "transfer-encoding"})
_SCHEMA_MANAGED_HEADERS = _REQUEST_MANAGED_HEADERS | frozenset(
    {"if-modified-since", "if-none-match"}
)
HTTP_STATUS_MINIMUM = 100
HTTP_STATUS_MAXIMUM = 599
MAX_EXPECTED_STATUSES = 100
MAX_REQUEST_HEADERS = 200
MAX_HEADER_NAME_LENGTH = 256
MAX_HEADER_VALUE_BYTES = 16_384
MAX_QUERY_KEYS = 500
MAX_QUERY_NAME_BYTES = 1_024
MAX_QUERY_VALUE_BYTES = 16_384
MAX_QUERY_VALUES = 10_000


class HttpMethod(StrEnum):
    GET = "GET"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class StatusRange(StrictModel):
    """Inclusive response status range."""

    minimum: int = Field(ge=HTTP_STATUS_MINIMUM, le=HTTP_STATUS_MAXIMUM)
    maximum: int = Field(ge=HTTP_STATUS_MINIMUM, le=HTTP_STATUS_MAXIMUM)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("Status range minimum cannot exceed maximum")
        return self


StatusExpectation = int | list[int] | StatusRange
QueryScalar = str | int | float | bool | None


def _utf8_size(value: str, *, label: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{label} must be valid UTF-8 text") from None


def _validated_headers(
    value: dict[str, str],
    *,
    managed: frozenset[str],
) -> dict[str, str]:
    seen: set[str] = set()
    for name, item in value.items():
        folded = name.casefold()
        if folded in seen:
            raise ValueError("HTTP header names must be unique ignoring case")
        seen.add(folded)
        if len(name) > MAX_HEADER_NAME_LENGTH:
            raise ValueError("HTTP header name exceeds its length limit")
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError("Invalid HTTP header name")
        if folded in managed:
            raise ValueError(f"Header '{name}' is managed by the HTTP client")
        if "\r" in item or "\n" in item:
            raise ValueError(f"Header '{name}' contains a prohibited line break")
        if _utf8_size(item, label="HTTP header value") > MAX_HEADER_VALUE_BYTES:
            raise ValueError("HTTP header value exceeds its byte limit")
    return value


def _validated_query(
    value: dict[str, QueryScalar | list[QueryScalar]],
) -> dict[str, QueryScalar | list[QueryScalar]]:
    total_values = 0
    for name, raw in value.items():
        if _utf8_size(name, label="Query name") > MAX_QUERY_NAME_BYTES:
            raise ValueError("Query name exceeds its byte limit")
        items = raw if isinstance(raw, list) else [raw]
        total_values += len(items)
        if total_values > MAX_QUERY_VALUES:
            raise ValueError("Query parameters exceed their aggregate value limit")
        for item in items:
            if isinstance(item, str) and (
                _utf8_size(item, label="Query value") > MAX_QUERY_VALUE_BYTES
            ):
                raise ValueError("Query value exceeds its byte limit")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Query numbers must be finite")
    return value


class RequestBase(StrictModel):
    """Shared safe request parameters."""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_REQUEST_HEADERS)
    query: dict[str, QueryScalar | list[QueryScalar]] = Field(
        default_factory=dict,
        max_length=MAX_QUERY_KEYS,
    )
    body: Any = None
    body_format: Literal["auto", "json", "form", "text"] = "auto"
    file: str | None = Field(default=None, max_length=500)
    expected_status: StatusExpectation = 200
    expected_message: str | None = Field(default=None, max_length=8_192)
    log_response_body: bool = True
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validated_headers(value, managed=_REQUEST_MANAGED_HEADERS)

    @field_validator("query")
    @classmethod
    def safe_query(
        cls,
        value: dict[str, QueryScalar | list[QueryScalar]],
    ) -> dict[str, QueryScalar | list[QueryScalar]]:
        return _validated_query(value)

    @field_validator("expected_status")
    @classmethod
    def valid_statuses(cls, value: StatusExpectation) -> StatusExpectation:
        if isinstance(value, int) and not HTTP_STATUS_MINIMUM <= value <= HTTP_STATUS_MAXIMUM:
            raise ValueError("Expected status must be between 100 and 599")
        if isinstance(value, list) and (
            not value
            or len(value) > MAX_EXPECTED_STATUSES
            or any(not HTTP_STATUS_MINIMUM <= item <= HTTP_STATUS_MAXIMUM for item in value)
        ):
            raise ValueError("Expected status list must contain 1-100 valid HTTP statuses")
        return value

    @field_validator("expected_message")
    @classmethod
    def meaningful_expected_message(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("expectedMessage cannot be blank")
        return value

    @model_validator(mode="after")
    def one_body_source(self) -> Self:
        if self.file is not None and self.body is not None:
            raise ValueError("Use either inline body or file, not both")
        return self


class SendRequestParams(RequestBase):
    """Parameters for an arbitrary validated HTTP request."""

    endpoint: str = Field(min_length=1, max_length=8_192)
    method: HttpMethod = HttpMethod.GET
    base_url: str | None = Field(default=None, max_length=8_192)


class ApiResponseResult(StrictModel):
    """Bounded response stored in scenario context for later expressions."""

    success: bool = True
    request_url: str
    method: HttpMethod
    request_body: Any = None
    status_code: int
    headers: dict[str, str]
    content_type: str | None = None
    response_body: Any = None
    response_bytes: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)


class LoadApiSchemaParams(StrictModel):
    """Download, validate, and cache a Swagger 2 or OpenAPI 3 document."""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    schema_url: str = Field(min_length=1, max_length=8_192)
    headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_REQUEST_HEADERS)

    @field_validator("headers")
    @classmethod
    def safe_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validated_headers(value, managed=_SCHEMA_MANAGED_HEADERS)


class ApiOperationSummary(StrictModel):
    """Compact operation metadata safe to expose to generation skills."""

    operation_id: str | None = None
    method: HttpMethod
    path: str
    summary: str | None = None


class LoadApiSchemaResult(StrictModel):
    """Cached schema identity and operation catalog."""

    success: bool = True
    schema_id: str
    schema_version: str
    source_url: str
    base_url: str
    cache_file: str
    operations: list[ApiOperationSummary]


class CallSchemaParams(RequestBase):
    """Invoke one operation selected from a Swagger/OpenAPI document."""

    schema_url: str | None = Field(default=None, max_length=8_192)
    schema_id: str | None = Field(default=None, min_length=64, max_length=64)
    schema_headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_REQUEST_HEADERS)
    operation_id: str | None = Field(default=None, max_length=300)
    path: str | None = Field(default=None, max_length=2_000)
    method: HttpMethod | None = None
    path_params: dict[str, QueryScalar] = Field(default_factory=dict, max_length=100)
    validate_request: bool = True
    validate_response: bool = True

    @field_validator("schema_headers")
    @classmethod
    def safe_schema_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validated_headers(value, managed=_SCHEMA_MANAGED_HEADERS)

    @model_validator(mode="after")
    def select_schema_and_operation(self) -> Self:
        if (self.schema_url is None) == (self.schema_id is None):
            raise ValueError("Provide exactly one of schemaUrl or schemaId")
        if self.schema_id is not None and self.schema_headers:
            raise ValueError("schemaHeaders can be used only with schemaUrl")
        by_id = self.operation_id is not None
        by_route = self.path is not None or self.method is not None
        if by_id == by_route:
            raise ValueError("Select an operation by operationId or by both path and method")
        if by_route and (self.path is None or self.method is None):
            raise ValueError("Both path and method are required for route-based operation lookup")
        return self


class ValidateSchemaParams(StrictModel):
    """Validate a prior response body against an operation response schema."""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    schema_url: str | None = Field(default=None, max_length=8_192)
    schema_id: str | None = Field(default=None, min_length=64, max_length=64)
    schema_headers: dict[str, str] = Field(default_factory=dict, max_length=MAX_REQUEST_HEADERS)
    operation_id: str | None = Field(default=None, max_length=300)
    path: str | None = Field(default=None, max_length=2_000)
    method: HttpMethod | None = None
    status_code: int = Field(ge=HTTP_STATUS_MINIMUM, le=HTTP_STATUS_MAXIMUM)
    content_type: str | None = Field(default=None, max_length=255)
    body: Any
    expected_valid: bool = True

    @field_validator("schema_headers")
    @classmethod
    def safe_schema_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _validated_headers(value, managed=_SCHEMA_MANAGED_HEADERS)

    @model_validator(mode="after")
    def select_schema_and_operation(self) -> Self:
        if (self.schema_url is None) == (self.schema_id is None):
            raise ValueError("Provide exactly one of schemaUrl or schemaId")
        if self.schema_id is not None and self.schema_headers:
            raise ValueError("schemaHeaders can be used only with schemaUrl")
        by_id = self.operation_id is not None
        by_route = self.path is not None or self.method is not None
        if by_id == by_route:
            raise ValueError("Select an operation by operationId or by both path and method")
        if by_route and (self.path is None or self.method is None):
            raise ValueError("Both path and method are required for route-based operation lookup")
        return self


class ValidateSchemaResult(StrictModel):
    """Successful schema validation result."""

    success: bool = True
    schema_id: str
    operation_id: str | None = None
    method: HttpMethod
    path: str
    status_code: int
    expected_valid: bool
    schema_valid: bool
    schema_path: str | None = None
    schema_rule: str | None = None
