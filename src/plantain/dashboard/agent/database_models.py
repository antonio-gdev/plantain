"""Strict backend models for progressive dashboard database discovery."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import AliasChoices, Field, model_validator

from plantain.models.common import StrictModel
from plantain.models.database import (
    DEFAULT_METADATA_PAGE_SIZE,
    DatabaseDiscoveryResult,
    DiscoveryPhase,
)

DATABASE_DISCOVERY_OUTPUT = "discovery"
MAX_DATABASE_EVIDENCE_BYTES = 131_072
MAX_DATABASE_EVIDENCE_ITEMS = 1_000
SUPPORTED_DATABASE_DIALECTS = frozenset({"mssql", "mysql", "oracle", "postgresql"})
DatabaseRunId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
EnvironmentReference = Annotated[
    str,
    Field(min_length=5, max_length=256, pattern=r"^env:[A-Za-z_][A-Za-z0-9_]*$"),
]


class DatabaseSourceReferences(StrictModel):
    """Unresolved database source names safe to retain as agent context."""

    username: EnvironmentReference
    password: EnvironmentReference
    db_url: EnvironmentReference = Field(
        validation_alias=AliasChoices("dbUrl", "db_url"),
        serialization_alias="dbUrl",
    )


class DatabaseDiscoveryRequest(StrictModel):
    """Non-secret request scope required for a faithful discovery continuation."""

    phase: DiscoveryPhase
    schema_name: str | None = Field(
        default=None,
        serialization_alias="schema",
    )
    table: str | None = None
    include_views: bool = Field(default=False, serialization_alias="includeViews")
    include_system_schemas: bool = Field(
        default=False,
        serialization_alias="includeSystemSchemas",
    )
    page_size: int = Field(
        default=DEFAULT_METADATA_PAGE_SIZE,
        ge=1,
        le=1_000,
        serialization_alias="pageSize",
    )


class DatabaseRunEvidence(StrictModel):
    """Verified metadata-only discovery evidence retained outside Reflex state."""

    run_id: DatabaseRunId
    scenario: str = Field(min_length=1, max_length=256)
    source_path: str = Field(min_length=1, max_length=2_048)
    source: DatabaseSourceReferences
    request: DatabaseDiscoveryRequest
    discovery: DatabaseDiscoveryResult = Field(repr=False)

    @model_validator(mode="after")
    def valid_evidence(self) -> Self:
        """Require complete, truthful, bounded metadata evidence."""

        if self.discovery.dialect not in SUPPORTED_DATABASE_DIALECTS:
            raise ValueError("Database discovery dialect is unsupported")
        _validate_request_scope(self.request, self.discovery)
        _validate_discovery_shape(self.discovery)
        encoded = self.model_dump_json(by_alias=True).encode()
        if len(encoded) > MAX_DATABASE_EVIDENCE_BYTES:
            raise ValueError("Database discovery evidence exceeds its byte limit")
        return self


def _validate_request_scope(
    request: DatabaseDiscoveryRequest,
    result: DatabaseDiscoveryResult,
) -> None:
    if request.phase is not result.phase or request.schema_name != result.schema_name:
        raise ValueError("Database discovery request and result scopes do not match")
    if result.table_metadata is not None and request.table != result.table_metadata.name:
        raise ValueError("Database table request and result scopes do not match")


def _validate_discovery_shape(result: DatabaseDiscoveryResult) -> None:
    if result.has_more != (result.next_cursor is not None):
        raise ValueError("Database discovery pagination is inconsistent")
    if result.next_cursor == "":
        raise ValueError("Database discovery cursor is invalid")
    if len(result.schemas) > MAX_DATABASE_EVIDENCE_ITEMS:
        raise ValueError("Database schema evidence exceeds its item limit")
    if len(result.objects) > MAX_DATABASE_EVIDENCE_ITEMS:
        raise ValueError("Database object evidence exceeds its item limit")
    if result.phase is DiscoveryPhase.SCHEMAS:
        _validate_schema_page(result)
    elif result.phase is DiscoveryPhase.TABLES:
        _validate_object_page(result)
    else:
        _validate_table_metadata(result)


def _validate_schema_page(result: DatabaseDiscoveryResult) -> None:
    if (
        result.schema_name is not None
        or result.objects
        or result.table_metadata is not None
        or result.item_count != len(result.schemas)
    ):
        raise ValueError("Database schema discovery evidence is inconsistent")


def _validate_object_page(result: DatabaseDiscoveryResult) -> None:
    if (
        not result.schema_name
        or result.schemas
        or result.table_metadata is not None
        or result.item_count != len(result.objects)
    ):
        raise ValueError("Database object discovery evidence is inconsistent")


def _validate_table_metadata(result: DatabaseDiscoveryResult) -> None:
    if (
        result.schemas
        or result.objects
        or result.table_metadata is None
        or result.item_count != 1
        or result.has_more
    ):
        raise ValueError("Database table discovery evidence is inconsistent")


__all__ = [
    "DATABASE_DISCOVERY_OUTPUT",
    "MAX_DATABASE_EVIDENCE_BYTES",
    "MAX_DATABASE_EVIDENCE_ITEMS",
    "SUPPORTED_DATABASE_DIALECTS",
    "DatabaseDiscoveryRequest",
    "DatabaseRunEvidence",
    "DatabaseRunId",
    "DatabaseSourceReferences",
    "EnvironmentReference",
]
