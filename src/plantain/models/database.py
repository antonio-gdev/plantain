"""Strict YAML contracts for database discovery, querying, and verification."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Self

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator

from plantain.models.common import StrictModel

DEFAULT_METADATA_PAGE_SIZE = 250
MAX_DATABASE_PARAMETERS = 1_000
_PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class DatabaseSource(StrictModel):
    """Credentials and a JDBC target resolved from YAML environment references."""

    username: str = Field(min_length=1, max_length=512)
    password: SecretStr
    db_url: str = Field(
        min_length=1,
        max_length=4_096,
        validation_alias=AliasChoices("dbUrl", "db_url"),
        serialization_alias="dbUrl",
    )

    @field_validator("db_url", mode="before")
    @classmethod
    def reject_ambiguous_db_url(cls, value: Any) -> Any:
        if isinstance(value, str) and (value != value.strip() or _CONTROL_CHARACTER.search(value)):
            raise ValueError("Database dbUrl contains prohibited whitespace or controls")
        return value


class DiscoveryPhase(StrEnum):
    """The three intentionally scoped metadata discovery phases."""

    SCHEMAS = "schemas"
    TABLES = "tables"
    TABLE = "table"


class DiscoverDatabaseParams(StrictModel):
    """Discover schemas, objects in one schema, or one object's metadata."""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    source: DatabaseSource
    phase: DiscoveryPhase
    schema_name: str | None = Field(
        default=None,
        max_length=512,
        validation_alias=AliasChoices("schema", "schemaName", "schema_name"),
        serialization_alias="schema",
    )
    table: str | None = Field(default=None, max_length=512)
    include_views: bool = Field(
        default=False,
        validation_alias=AliasChoices("includeViews", "include_views"),
        serialization_alias="includeViews",
    )
    include_system_schemas: bool = Field(
        default=False,
        validation_alias=AliasChoices("includeSystemSchemas", "include_system_schemas"),
        serialization_alias="includeSystemSchemas",
    )
    page_size: int = Field(
        default=DEFAULT_METADATA_PAGE_SIZE,
        ge=1,
        le=1_000,
        validation_alias=AliasChoices("pageSize", "page_size"),
        serialization_alias="pageSize",
    )
    cursor: str | None = Field(default=None, min_length=1, max_length=4_096)

    @model_validator(mode="after")
    def phase_scope(self) -> Self:
        if self.phase is DiscoveryPhase.SCHEMAS:
            if self.schema_name is not None or self.table is not None or self.include_views:
                raise ValueError("schemas phase accepts pagination and includeSystemSchemas only")
        elif self.phase is DiscoveryPhase.TABLES:
            if self.schema_name is None:
                raise ValueError("tables phase requires schema")
            if self.table is not None:
                raise ValueError("tables phase does not accept table")
            if self.include_system_schemas:
                raise ValueError("includeSystemSchemas is valid only for schemas phase")
        else:
            if self.table is None:
                raise ValueError("table phase requires table")
            if self.include_system_schemas:
                raise ValueError("includeSystemSchemas is valid only for schemas phase")
            if self.cursor is not None or self.page_size != DEFAULT_METADATA_PAGE_SIZE:
                raise ValueError("table phase returns one complete object and is not paginated")
        return self


class DatabaseObject(StrictModel):
    """Discovered table or view identity."""

    name: str
    type: str


class DatabaseColumn(StrictModel):
    """Portable reflected column metadata."""

    name: str
    type: str
    nullable: bool
    ordinal_position: int = Field(
        ge=1,
        validation_alias=AliasChoices("ordinalPosition", "ordinal_position"),
        serialization_alias="ordinalPosition",
    )
    default: str | None = None
    primary_key: bool = Field(
        default=False,
        validation_alias=AliasChoices("primaryKey", "primary_key"),
        serialization_alias="primaryKey",
    )
    autoincrement: bool | str | None = None
    comment: str | None = None
    computed: dict[str, Any] | None = None
    identity: dict[str, Any] | None = None


class DatabasePrimaryKey(StrictModel):
    """Portable primary-key metadata."""

    name: str | None = None
    columns: list[str] = Field(default_factory=list)


class DatabaseForeignKey(StrictModel):
    """Portable foreign-key metadata."""

    name: str | None = None
    constrained_columns: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("constrainedColumns", "constrained_columns"),
        serialization_alias="constrainedColumns",
    )
    referred_schema: str | None = Field(
        default=None,
        validation_alias=AliasChoices("referredSchema", "referred_schema"),
        serialization_alias="referredSchema",
    )
    referred_table: str = Field(
        validation_alias=AliasChoices("referredTable", "referred_table"),
        serialization_alias="referredTable",
    )
    referred_columns: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("referredColumns", "referred_columns"),
        serialization_alias="referredColumns",
    )
    options: dict[str, Any] = Field(default_factory=dict)


class DatabaseIndex(StrictModel):
    """Portable index metadata."""

    name: str | None = None
    columns: list[str | None] = Field(default_factory=list)
    unique: bool = False
    expressions: list[str] = Field(default_factory=list)


class DatabaseUniqueConstraint(StrictModel):
    """Portable unique-constraint metadata."""

    name: str | None = None
    columns: list[str] = Field(default_factory=list)


class DatabaseCheckConstraint(StrictModel):
    """Portable check-constraint metadata."""

    name: str | None = None
    sql_text: str = Field(
        validation_alias=AliasChoices("sqlText", "sql_text"),
        serialization_alias="sqlText",
    )


class DatabaseTableMetadata(StrictModel):
    """Complete metadata for one explicitly selected table or view."""

    name: str
    type: str
    comment: str | None = None
    columns: list[DatabaseColumn] = Field(default_factory=list)
    primary_key: DatabasePrimaryKey = Field(
        default_factory=DatabasePrimaryKey,
        validation_alias=AliasChoices("primaryKey", "primary_key"),
        serialization_alias="primaryKey",
    )
    foreign_keys: list[DatabaseForeignKey] = Field(
        default_factory=list,
        validation_alias=AliasChoices("foreignKeys", "foreign_keys"),
        serialization_alias="foreignKeys",
    )
    indexes: list[DatabaseIndex] = Field(default_factory=list)
    unique_constraints: list[DatabaseUniqueConstraint] = Field(
        default_factory=list,
        validation_alias=AliasChoices("uniqueConstraints", "unique_constraints"),
        serialization_alias="uniqueConstraints",
    )
    check_constraints: list[DatabaseCheckConstraint] = Field(
        default_factory=list,
        validation_alias=AliasChoices("checkConstraints", "check_constraints"),
        serialization_alias="checkConstraints",
    )


class DatabaseDiscoveryResult(StrictModel):
    """One metadata page or one complete targeted object."""

    success: bool = True
    phase: DiscoveryPhase
    dialect: str
    schema_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("schema", "schemaName", "schema_name"),
        serialization_alias="schema",
    )
    schemas: list[str] = Field(default_factory=list)
    objects: list[DatabaseObject] = Field(default_factory=list)
    table_metadata: DatabaseTableMetadata | None = Field(
        default=None,
        validation_alias=AliasChoices("tableMetadata", "table_metadata"),
        serialization_alias="tableMetadata",
    )
    item_count: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("itemCount", "item_count"),
        serialization_alias="itemCount",
    )
    has_more: bool = Field(
        default=False,
        validation_alias=AliasChoices("hasMore", "has_more"),
        serialization_alias="hasMore",
    )
    next_cursor: str | None = Field(
        default=None,
        validation_alias=AliasChoices("nextCursor", "next_cursor"),
        serialization_alias="nextCursor",
    )


class DatabaseResultMode(StrEnum):
    """Shape returned in the canonical query result field."""

    VALUE = "value"
    ROW = "row"
    ROWS = "rows"


class QueryDatabaseParams(StrictModel):
    """Execute one explicit parameterized read-only query."""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    source: DatabaseSource
    sql: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    file: str | None = Field(default=None, min_length=1, max_length=1_024)
    parameters: dict[str, Any] = Field(default_factory=dict, max_length=MAX_DATABASE_PARAMETERS)
    result_mode: DatabaseResultMode | None = Field(
        default=None,
        validation_alias=AliasChoices("resultMode", "result_mode"),
        serialization_alias="resultMode",
    )
    single_result: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("singleResult", "single_result"),
        serialization_alias="singleResult",
    )
    max_rows: int | None = Field(
        default=None,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices("maxRows", "max_rows"),
        serialization_alias="maxRows",
    )

    @field_validator("parameters")
    @classmethod
    def valid_parameter_names(cls, value: dict[str, Any]) -> dict[str, Any]:
        invalid = sorted(name for name in value if _PARAMETER_NAME.fullmatch(name) is None)
        if invalid:
            raise ValueError("Database bind parameter names must be SQL identifiers")
        return value

    @model_validator(mode="after")
    def one_statement_source(self) -> Self:
        if (self.sql is None) == (self.file is None):
            raise ValueError("queryDatabase requires exactly one of sql or file")
        if self.result_mode is not None and self.single_result is not None:
            raise ValueError("queryDatabase cannot combine resultMode with legacy singleResult")
        if self.effective_result_mode is not DatabaseResultMode.ROWS and self.max_rows is not None:
            raise ValueError("maxRows is valid only when resultMode is rows")
        return self

    @property
    def effective_result_mode(self) -> DatabaseResultMode:
        """Resolve the canonical mode."""

        if self.result_mode is not None:
            return self.result_mode
        return DatabaseResultMode.ROW if self.single_result else DatabaseResultMode.ROWS


class QueryDatabaseResult(StrictModel):
    """One bounded chaining value plus non-sensitive execution metadata."""

    success: bool = True
    dialect: str
    columns: list[str] = Field(default_factory=list)
    result: Any = None
    row_count: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("rowCount", "row_count"),
        serialization_alias="rowCount",
    )
    truncated: bool = False
    result_mode: DatabaseResultMode = Field(
        validation_alias=AliasChoices("resultMode", "result_mode"),
        serialization_alias="resultMode",
    )


class DatabaseMatchMode(StrEnum):
    """Supported verification semantics."""

    CONTAINS = "contains"
    EXACT = "exact"
    UNORDERED_CONTAINS = "unorderedContains"


class VerifyDatabaseResultParams(StrictModel):
    """Verify a prior query result without another database call."""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    actual: Any
    expected: Any
    match_mode: DatabaseMatchMode = Field(
        default=DatabaseMatchMode.CONTAINS,
        validation_alias=AliasChoices("matchMode", "match_mode"),
        serialization_alias="matchMode",
    )


class VerifyDatabaseResult(StrictModel):
    """Successful database verification summary."""

    success: bool = True
    match_mode: DatabaseMatchMode = Field(
        validation_alias=AliasChoices("matchMode", "match_mode"),
        serialization_alias="matchMode",
    )
    matched_rows: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("matchedRows", "matched_rows"),
        serialization_alias="matchedRows",
    )


__all__ = [
    "DatabaseCheckConstraint",
    "DatabaseColumn",
    "DatabaseDiscoveryResult",
    "DatabaseForeignKey",
    "DatabaseIndex",
    "DatabaseMatchMode",
    "DatabaseObject",
    "DatabasePrimaryKey",
    "DatabaseResultMode",
    "DatabaseSource",
    "DatabaseTableMetadata",
    "DatabaseUniqueConstraint",
    "DiscoverDatabaseParams",
    "DiscoveryPhase",
    "QueryDatabaseParams",
    "QueryDatabaseResult",
    "VerifyDatabaseResult",
    "VerifyDatabaseResultParams",
]
