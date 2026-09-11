"""Strict models for dashboard-guided Swagger and OpenAPI workflows."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from plantain.models.api import HttpMethod
from plantain.models.common import StrictModel

MAX_API_CONTRACT_EVIDENCE_BYTES = 131_072
MAX_API_OPERATION_MATCHES = 12
MAX_API_OPERATION_QUERY_LENGTH = 240
MAX_API_PREVIEW_TEXT_LENGTH = 320
MAX_API_SCHEMA_HEADERS = 32
MAX_API_SCHEMA_REFERENCE_LENGTH = 2_048
_ENV_REFERENCE_PATTERN = r"^env:[A-Za-z_][A-Za-z0-9_]*$"
_HEADER_NAME_PATTERN = r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$"
_INSPECTION_ID_PATTERN = r"^[a-f0-9]{32}$"
_OPERATION_KEY_PATTERN = r"^[a-f0-9]{64}$"
_SCHEMA_ID_PATTERN = r"^[a-f0-9]{64}$"

ApiOperationQuery = Annotated[
    str,
    Field(min_length=1, max_length=MAX_API_OPERATION_QUERY_LENGTH),
]
ApiSchemaReference = Annotated[
    str,
    Field(min_length=1, max_length=MAX_API_SCHEMA_REFERENCE_LENGTH),
]


class ApiWorkflowKind(StrEnum):
    """API intent paths selected internally by the natural-language router."""

    CONTRACT = "contract"
    REQUEST = "request"


class ApiSchemaHeaderReference(StrictModel):
    """One schema-download header whose value remains an environment reference."""

    name: str = Field(min_length=1, max_length=256, pattern=_HEADER_NAME_PATTERN)
    value: str = Field(min_length=5, max_length=256, pattern=_ENV_REFERENCE_PATTERN)


class ApiOperationCandidate(StrictModel):
    """Browser-safe metadata for one locally verified API operation."""

    operation_key: str = Field(pattern=_OPERATION_KEY_PATTERN)
    operation_id: str = Field(max_length=MAX_API_PREVIEW_TEXT_LENGTH)
    method: HttpMethod
    path: str = Field(min_length=1, max_length=MAX_API_PREVIEW_TEXT_LENGTH)
    summary: str = Field(max_length=MAX_API_PREVIEW_TEXT_LENGTH)
    display_limited: bool = False


class ApiContractInspection(StrictModel):
    """Bounded schema and operation-selection result safe for Reflex state."""

    inspection_id: str | None = Field(default=None, pattern=_INSPECTION_ID_PATTERN)
    schema_id: str = Field(pattern=_SCHEMA_ID_PATTERN)
    schema_version: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1, max_length=MAX_API_SCHEMA_REFERENCE_LENGTH)
    base_url: str = Field(min_length=1, max_length=MAX_API_SCHEMA_REFERENCE_LENGTH)
    query: ApiOperationQuery
    operations: list[ApiOperationCandidate] = Field(
        default_factory=list,
        max_length=MAX_API_OPERATION_MATCHES,
    )
    total_matches: int = Field(ge=0)
    matches_limited: bool = False

    @model_validator(mode="after")
    def valid_match_count(self) -> Self:
        """Require a truthful operation-preview limit signal."""

        if self.total_matches < len(self.operations):
            raise ValueError("API operation match count is invalid")
        if self.matches_limited != (self.total_matches > len(self.operations)):
            raise ValueError("API operation limit signal is invalid")
        return self


class ApiContractEvidence(StrictModel):
    """Backend-only bounded contract packet for one exact operation."""

    schema_id: str = Field(pattern=_SCHEMA_ID_PATTERN)
    operation_key: str = Field(pattern=_OPERATION_KEY_PATTERN)
    content: str = Field(
        min_length=1,
        max_length=MAX_API_CONTRACT_EVIDENCE_BYTES,
        repr=False,
    )


def validate_schema_headers(
    values: list[ApiSchemaHeaderReference],
) -> list[ApiSchemaHeaderReference]:
    """Reject case-insensitive duplicates in provider-selected schema headers."""

    names = [item.name.casefold() for item in values]
    if len(names) != len(set(names)):
        raise ValueError("Schema header names must be unique")
    if len(values) > MAX_API_SCHEMA_HEADERS:
        raise ValueError("Too many schema headers were selected")
    return values


__all__ = [
    "MAX_API_CONTRACT_EVIDENCE_BYTES",
    "MAX_API_OPERATION_MATCHES",
    "ApiContractEvidence",
    "ApiContractInspection",
    "ApiOperationCandidate",
    "ApiOperationQuery",
    "ApiSchemaHeaderReference",
    "ApiSchemaReference",
    "ApiWorkflowKind",
    "validate_schema_headers",
]
