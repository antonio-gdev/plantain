"""Portable server-bounded catalog name queries with Inspector fallback signaling."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import and_, case, column, func, or_, select, table
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from plantain.activities.database.errors import DatabaseActivityError

CatalogObject = tuple[str, str]
SYSTEM_SCHEMAS = frozenset(
    {
        "db_accessadmin",
        "db_backupoperator",
        "db_datareader",
        "db_datawriter",
        "db_ddladmin",
        "db_denydatareader",
        "db_denydatawriter",
        "db_owner",
        "db_securityadmin",
        "information_schema",
        "mysql",
        "performance_schema",
        "pg_catalog",
        "sys",
    }
)
_SUPPORTED_DIALECTS = frozenset({"mssql", "mysql", "oracle", "postgresql"})
_TABLE_KINDS = ("BASE TABLE", "FOREIGN", "LOCAL TEMPORARY", "TABLE")
_VIEW_KINDS = ("MATERIALIZED VIEW", "VIEW")


def catalog_schema_page(
    connection: Connection,
    *,
    after: tuple[str, ...] | None,
    page_size: int,
    include_system_schemas: bool,
) -> list[str] | None:
    """Return at most one server-bounded page plus a lookahead item."""

    columns = _schema_columns(connection.dialect.name)
    if columns is None or not hasattr(connection, "execute"):
        return None
    source, name = columns
    statement = select(name.label("name")).select_from(source)
    if not include_system_schemas:
        statement = statement.where(func.lower(name).not_in(tuple(sorted(SYSTEM_SCHEMAS))))
    after_clause = _after_name(name, after)
    if after_clause is not None:
        statement = statement.where(after_clause)
    statement = statement.order_by(func.lower(name), name).limit(page_size + 1)
    rows = _execute(connection, statement)
    return None if rows is None else _names(rows)


def catalog_schema_matches(
    connection: Connection,
    requested: str,
) -> list[str] | None:
    """Filter one schema name at the server for exact case resolution."""

    columns = _schema_columns(connection.dialect.name)
    if columns is None or not hasattr(connection, "execute"):
        return None
    source, name = columns
    statement = (
        select(name.label("name"))
        .select_from(source)
        .where(func.lower(name) == requested.casefold())
        .order_by(name)
        .limit(2)
    )
    rows = _execute(connection, statement)
    return None if rows is None else _names(rows)


def catalog_object_page(
    connection: Connection,
    schema: str,
    *,
    after: tuple[str, ...] | None,
    page_size: int,
    include_views: bool,
) -> list[CatalogObject] | None:
    """Return one bounded table/view page plus a lookahead item."""

    statement = _object_statement(
        connection,
        schema,
        include_views=include_views,
        after=after,
        requested=None,
    )
    if statement is None:
        return None
    rows = _execute(connection, statement.limit(page_size + 1))
    return None if rows is None else _objects(rows)


def catalog_object_matches(
    connection: Connection,
    schema: str,
    requested: str,
    *,
    include_views: bool,
) -> list[CatalogObject] | None:
    """Filter one table/view name at the server for exact case resolution."""

    statement = _object_statement(
        connection,
        schema,
        include_views=include_views,
        after=None,
        requested=requested,
    )
    if statement is None:
        return None
    rows = _execute(connection, statement.limit(2))
    return None if rows is None else _objects(rows)


def _object_statement(
    connection: Connection,
    schema: str,
    *,
    include_views: bool,
    after: tuple[str, ...] | None,
    requested: str | None,
) -> Any | None:
    columns = _object_columns(connection.dialect.name)
    if columns is None or not hasattr(connection, "execute"):
        return None
    source, name, owner, raw_kind = columns
    normalized_kind = func.upper(raw_kind)
    kinds = _TABLE_KINDS + (_VIEW_KINDS if include_views else ())
    kind = case((normalized_kind.in_(_VIEW_KINDS), "view"), else_="table")
    statement = (
        select(name.label("name"), kind.label("type"))
        .select_from(source)
        .where(owner == schema, normalized_kind.in_(kinds))
    )
    if requested is not None:
        statement = statement.where(func.lower(name) == requested.casefold())
    after_clause = _after_name(name, after, kind=kind)
    if after_clause is not None:
        statement = statement.where(after_clause)
    return statement.order_by(func.lower(name), name, kind)


def _schema_columns(dialect: str) -> tuple[Any, Any] | None:
    if dialect not in _SUPPORTED_DIALECTS:
        return None
    if dialect == "oracle":
        source = table("all_users", column("username"))
        return source, source.c.username
    source = table(
        "schemata",
        column("schema_name"),
        schema="information_schema",
    )
    return source, source.c.schema_name


def _object_columns(dialect: str) -> tuple[Any, Any, Any, Any] | None:
    if dialect not in _SUPPORTED_DIALECTS:
        return None
    if dialect == "oracle":
        source = table(
            "all_objects",
            column("owner"),
            column("object_name"),
            column("object_type"),
        )
        return source, source.c.object_name, source.c.owner, source.c.object_type
    source = table(
        "tables",
        column("table_schema"),
        column("table_name"),
        column("table_type"),
        schema="information_schema",
    )
    return source, source.c.table_name, source.c.table_schema, source.c.table_type


def _after_name(name: Any, after: tuple[str, ...] | None, *, kind: Any = None) -> Any:
    if after is None:
        return None
    expected_length = 3 if kind is not None else 2
    if len(after) != expected_length:
        raise DatabaseActivityError("Database discovery cursor does not match this request")
    folded = func.lower(name)
    clauses = [
        folded > after[0],
        and_(folded == after[0], name > after[1]),
    ]
    if kind is not None:
        clauses.append(and_(folded == after[0], name == after[1], kind > after[2]))
    return or_(*clauses)


def _execute(connection: Connection, statement: Any) -> list[Mapping[str, Any]] | None:
    try:
        return [cast("Mapping[str, Any]", row) for row in connection.execute(statement).mappings()]
    except SQLAlchemyError:
        return None


def _names(rows: list[Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise DatabaseActivityError("Database returned invalid schema catalog metadata")
        names.append(name)
    return names


def _objects(rows: list[Mapping[str, Any]]) -> list[CatalogObject]:
    objects: list[CatalogObject] = []
    for row in rows:
        name = row.get("name")
        kind = row.get("type")
        if not isinstance(name, str) or not name or kind not in {"table", "view"}:
            raise DatabaseActivityError("Database returned invalid object catalog metadata")
        objects.append((name, kind))
    return objects


__all__ = [
    "SYSTEM_SCHEMAS",
    "CatalogObject",
    "catalog_object_matches",
    "catalog_object_page",
    "catalog_schema_matches",
    "catalog_schema_page",
]
