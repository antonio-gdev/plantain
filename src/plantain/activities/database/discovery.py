"""Scoped SQLAlchemy Inspector metadata discovery with opaque pagination."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar, cast

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

from plantain.activities.database.catalog import (
    SYSTEM_SCHEMAS,
    catalog_object_matches,
    catalog_object_page,
    catalog_schema_matches,
    catalog_schema_page,
)
from plantain.activities.database.errors import DatabaseActivityError
from plantain.models.database import (
    DatabaseCheckConstraint,
    DatabaseColumn,
    DatabaseDiscoveryResult,
    DatabaseForeignKey,
    DatabaseIndex,
    DatabaseObject,
    DatabasePrimaryKey,
    DatabaseTableMetadata,
    DatabaseUniqueConstraint,
    DiscoverDatabaseParams,
    DiscoveryPhase,
)

MAX_METADATA_TEXT_LENGTH = 100_000
_T = TypeVar("_T")


def discover_metadata(
    connection: Connection,
    params: DiscoverDatabaseParams,
    *,
    max_result_bytes: int,
) -> DatabaseDiscoveryResult:
    """Execute exactly one Inspector-only discovery phase."""

    inspector = inspect(connection)
    if params.phase is DiscoveryPhase.SCHEMAS:
        result = _schemas(connection, inspector, params)
    elif params.phase is DiscoveryPhase.TABLES:
        result = _tables(connection, inspector, params)
    else:
        result = _table(connection, inspector, params)
    encoded = result.model_dump_json(by_alias=True).encode("utf-8")
    if len(encoded) > max_result_bytes:
        raise DatabaseActivityError("Database metadata result exceeds PLANTAIN_DB_MAX_RESULT_BYTES")
    return result


def _schemas(
    connection: Connection,
    inspector: Inspector,
    params: DiscoverDatabaseParams,
) -> DatabaseDiscoveryResult:
    scope = "schemas"
    after = _decode_cursor(params.cursor, phase=params.phase, scope=scope)
    names = catalog_schema_page(
        connection,
        after=after,
        page_size=params.page_size,
        include_system_schemas=params.include_system_schemas,
    )
    if names is None:
        names = sorted(set(inspector.get_schema_names()), key=_name_key)
        if not params.include_system_schemas:
            names = [name for name in names if name.casefold() not in SYSTEM_SCHEMAS]
        page, next_cursor = _page(names, params=params, scope=scope, key=_name_key)
    else:
        page, next_cursor = _finish_page(names, params=params, scope=scope, key=_name_key)
    return DatabaseDiscoveryResult(
        phase=params.phase,
        dialect=connection.dialect.name,
        schemas=page,
        item_count=len(page),
        has_more=next_cursor is not None,
        next_cursor=next_cursor,
    )


def _tables(
    connection: Connection,
    inspector: Inspector,
    params: DiscoverDatabaseParams,
) -> DatabaseDiscoveryResult:
    available_schemas = (
        catalog_schema_matches(connection, params.schema_name)
        if params.schema_name is not None
        else None
    )
    schema = _resolve_name(
        inspector.get_schema_names() if available_schemas is None else available_schemas,
        params.schema_name,
        kind="schema",
    )
    scope = f"tables\0{schema}"
    after = _decode_cursor(params.cursor, phase=params.phase, scope=scope)
    catalog_objects = catalog_object_page(
        connection,
        schema,
        after=after,
        page_size=params.page_size,
        include_views=params.include_views,
    )
    if catalog_objects is None:
        objects = [
            DatabaseObject(name=name, type="table")
            for name in inspector.get_table_names(schema=schema)
        ]
        if params.include_views:
            objects.extend(
                DatabaseObject(name=name, type="view")
                for name in inspector.get_view_names(schema=schema)
            )
        objects.sort(key=_object_key)
        page, next_cursor = _page(objects, params=params, scope=scope, key=_object_key)
    else:
        objects = [DatabaseObject(name=name, type=kind) for name, kind in catalog_objects]
        page, next_cursor = _finish_page(objects, params=params, scope=scope, key=_object_key)
    return DatabaseDiscoveryResult(
        phase=params.phase,
        dialect=connection.dialect.name,
        schema_name=schema,
        objects=page,
        item_count=len(page),
        has_more=next_cursor is not None,
        next_cursor=next_cursor,
    )


def _table(
    connection: Connection,
    inspector: Inspector,
    params: DiscoverDatabaseParams,
) -> DatabaseDiscoveryResult:
    available_schemas = (
        catalog_schema_matches(connection, params.schema_name)
        if params.schema_name is not None
        else None
    )
    schemas = inspector.get_schema_names() if available_schemas is None else available_schemas
    schema = (
        _resolve_name(schemas, params.schema_name, kind="schema")
        if params.schema_name is not None
        else inspector.default_schema_name
    )
    catalog_objects = (
        catalog_object_matches(
            connection,
            schema,
            params.table,
            include_views=params.include_views,
        )
        if params.table is not None and schema is not None
        else None
    )
    if catalog_objects is None:
        tables = inspector.get_table_names(schema=schema)
        views = inspector.get_view_names(schema=schema) if params.include_views else []
    else:
        tables = [name for name, kind in catalog_objects if kind == "table"]
        views = [name for name, kind in catalog_objects if kind == "view"]
    object_type = "table"
    try:
        table_name = _resolve_name(tables, params.table, kind="table")
    except DatabaseActivityError:
        if not params.include_views:
            raise
        table_name = _resolve_name(views, params.table, kind="table or view")
        object_type = "view"

    primary_value = inspector.get_pk_constraint(table_name, schema=schema)
    primary_raw: Mapping[str, Any] = primary_value if isinstance(primary_value, Mapping) else {}
    primary_columns = [str(value) for value in primary_raw.get("constrained_columns") or []]
    primary_set = set(primary_columns)
    columns = [
        _column(raw, position=index, primary_keys=primary_set)
        for index, raw in enumerate(
            inspector.get_columns(table_name, schema=schema),
            start=1,
        )
    ]
    comment_raw: Mapping[str, Any] = _optional_inspection(
        inspector.get_table_comment,
        table_name,
        schema=schema,
        default={},
    )
    foreign_key_values: list[Mapping[str, Any]] = _optional_inspection(
        inspector.get_foreign_keys,
        table_name,
        schema=schema,
        default=[],
    )
    index_values: list[Mapping[str, Any]] = _optional_inspection(
        inspector.get_indexes,
        table_name,
        schema=schema,
        default=[],
    )
    unique_values: list[Mapping[str, Any]] = _optional_inspection(
        inspector.get_unique_constraints,
        table_name,
        schema=schema,
        default=[],
    )
    check_values: list[Mapping[str, Any]] = _optional_inspection(
        inspector.get_check_constraints,
        table_name,
        schema=schema,
        default=[],
    )
    metadata = DatabaseTableMetadata(
        name=table_name,
        type=object_type,
        comment=_bounded_text(comment_raw.get("text")) if comment_raw else None,
        columns=columns,
        primary_key=DatabasePrimaryKey(
            name=_bounded_text(primary_raw.get("name")),
            columns=primary_columns,
        ),
        foreign_keys=[_foreign_key(value) for value in foreign_key_values],
        indexes=[_index(value) for value in index_values],
        unique_constraints=[
            DatabaseUniqueConstraint(
                name=_bounded_text(value.get("name")),
                columns=[str(column) for column in value.get("column_names") or []],
            )
            for value in unique_values
        ],
        check_constraints=[
            DatabaseCheckConstraint(
                name=_bounded_text(value.get("name")),
                sql_text=_bounded_text(value.get("sqltext")) or "",
            )
            for value in check_values
            if value.get("sqltext") is not None
        ],
    )
    return DatabaseDiscoveryResult(
        phase=params.phase,
        dialect=connection.dialect.name,
        schema_name=schema,
        table_metadata=metadata,
        item_count=1,
    )


def _column(
    value: Mapping[str, Any],
    *,
    position: int,
    primary_keys: set[str],
) -> DatabaseColumn:
    name = str(value.get("name") or "")
    if not name:
        raise DatabaseActivityError("Database returned column metadata without a name")
    return DatabaseColumn(
        name=name,
        type=_bounded_text(value.get("type")) or "unknown",
        nullable=bool(value.get("nullable", True)),
        ordinal_position=position,
        default=_bounded_text(value.get("default")),
        primary_key=name in primary_keys,
        autoincrement=_portable(value.get("autoincrement")),
        comment=_bounded_text(value.get("comment")),
        computed=_portable_mapping(value.get("computed")),
        identity=_portable_mapping(value.get("identity")),
    )


def _foreign_key(value: Mapping[str, Any]) -> DatabaseForeignKey:
    referred_table = value.get("referred_table")
    if not referred_table:
        raise DatabaseActivityError("Database returned incomplete foreign-key metadata")
    return DatabaseForeignKey(
        name=_bounded_text(value.get("name")),
        constrained_columns=[str(item) for item in value.get("constrained_columns") or []],
        referred_schema=_bounded_text(value.get("referred_schema")),
        referred_table=str(referred_table),
        referred_columns=[str(item) for item in value.get("referred_columns") or []],
        options=_portable_mapping(value.get("options")) or {},
    )


def _index(value: Mapping[str, Any]) -> DatabaseIndex:
    expressions = value.get("expressions") or []
    return DatabaseIndex(
        name=_bounded_text(value.get("name")),
        columns=[None if item is None else str(item) for item in value.get("column_names") or []],
        unique=bool(value.get("unique", False)),
        expressions=[str(item) for item in expressions],
    )


def _optional_inspection(
    operation: Callable[..., Any],
    *args: object,
    default: _T,
    **kwargs: object,
) -> _T:
    try:
        return cast(_T, operation(*args, **kwargs))
    except NotImplementedError:
        return default


def _resolve_name(
    available: Sequence[str],
    requested: str | None,
    *,
    kind: str,
) -> str:
    if requested is None:
        raise DatabaseActivityError(f"Database {kind} is required")
    if requested in available:
        return requested
    matches = [value for value in available if value.casefold() == requested.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise DatabaseActivityError(f"Requested database {kind} is case-ambiguous")
    raise DatabaseActivityError(f"Requested database {kind} does not exist")


def _page(
    values: list[_T],
    *,
    params: DiscoverDatabaseParams,
    scope: str,
    key: Callable[[_T], tuple[str, ...]],
) -> tuple[list[_T], str | None]:
    after = _decode_cursor(params.cursor, phase=params.phase, scope=scope)
    remaining = [value for value in values if after is None or key(value) > after]
    return _finish_page(remaining, params=params, scope=scope, key=key)


def _finish_page(
    values: list[_T],
    *,
    params: DiscoverDatabaseParams,
    scope: str,
    key: Callable[[_T], tuple[str, ...]],
) -> tuple[list[_T], str | None]:
    has_more = len(values) > params.page_size
    page = values[: params.page_size]
    cursor = _encode_cursor(params.phase, scope, key(page[-1])) if has_more and page else None
    return page, cursor


def _encode_cursor(phase: DiscoveryPhase, scope: str, key: tuple[str, ...]) -> str:
    payload = json.dumps(
        {"version": 1, "phase": phase.value, "scope": scope, "key": key},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str | None,
    *,
    phase: DiscoveryPhase,
    scope: str,
) -> tuple[str, ...] | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatabaseActivityError("Database discovery cursor is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("phase") != phase.value
        or value.get("scope") != scope
        or not isinstance(value.get("key"), list)
        or not value["key"]
        or not all(isinstance(item, str) for item in value["key"])
    ):
        raise DatabaseActivityError("Database discovery cursor does not match this request")
    return tuple(value["key"])


def _name_key(value: str) -> tuple[str, ...]:
    return (value.casefold(), value)


def _object_key(value: DatabaseObject) -> tuple[str, ...]:
    return (value.name.casefold(), value.name, value.type)


def _portable_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return {"value": _portable(value)}
    return {str(key): _portable(item) for key, item in value.items()}


def _portable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _portable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_portable(item) for item in value]
    return _bounded_text(value)


def _bounded_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > MAX_METADATA_TEXT_LENGTH:
        raise DatabaseActivityError("Database metadata value exceeds the safety limit")
    return text


__all__ = ["discover_metadata"]
