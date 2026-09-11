"""Bounded, parameterized, read-only database query execution."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from plantain.activities.database.errors import DatabaseActivityError
from plantain.activities.database.policy import prepare_agent_query
from plantain.config import Settings
from plantain.errors import AtomicPersistenceError
from plantain.models.database import (
    DatabaseResultMode,
    QueryDatabaseParams,
    QueryDatabaseResult,
)
from plantain.persistence import open_binary_read_no_follow

MAX_BINARY_CELL_BYTES = 100_000
JSON_TEXT_CHUNK_CHARACTERS = 8_192
MAX_STRUCTURED_DEPTH = 20
MAX_STRUCTURED_ITEMS = 10_000
MAX_SQL_FILE_BYTES = 1_000_000
_BINARY_JSON_OVERHEAD_BYTES = len(b'{"$binaryBase64":""}')
_UNHANDLED = object()


class _CellLimitError(DatabaseActivityError):
    """Signals that one portable value cannot fit the remaining result budget."""


def execute_query(
    connection: Connection,
    params: QueryDatabaseParams,
    settings: Settings,
) -> QueryDatabaseResult:
    """Execute one proven SELECT and stream only a configured result bound."""

    statement = _statement(params, settings.project_root)
    dialect = connection.dialect.name
    mode = params.effective_result_mode
    max_rows = params.max_rows or settings.db_max_rows
    if max_rows > settings.db_max_rows:
        raise DatabaseActivityError("maxRows cannot exceed PLANTAIN_DB_MAX_ROWS")
    fetch_limit = 2 if mode is not DatabaseResultMode.ROWS else max_rows + 1
    bounded_statement = prepare_agent_query(
        statement,
        dialect=_sqlglot_dialect(dialect),
        row_limit=fetch_limit,
    )
    original_clause = text(statement)
    expected_parameters = set(original_clause._bindparams)  # noqa: SLF001
    supplied_parameters = set(params.parameters)
    if expected_parameters != supplied_parameters:
        missing = len(expected_parameters - supplied_parameters)
        unexpected = len(supplied_parameters - expected_parameters)
        raise DatabaseActivityError(
            "Database bind parameters do not match the query "
            f"(missing={missing}, unexpected={unexpected})"
        )
    clause = text(bounded_statement).execution_options(yield_per=settings.db_fetch_batch_size)
    result = connection.execute(clause, params.parameters)
    try:
        return _consume_query_result(
            result,
            dialect=dialect,
            mode=mode,
            max_rows=max_rows,
            fetch_limit=fetch_limit,
            settings=settings,
        )
    finally:
        result.close()


def _consume_query_result(
    result: Any,
    *,
    dialect: str,
    mode: DatabaseResultMode,
    max_rows: int,
    fetch_limit: int,
    settings: Settings,
) -> QueryDatabaseResult:
    if not result.returns_rows:
        raise DatabaseActivityError("queryDatabase SELECT did not return a row set")
    columns = list(map(str, result.keys()))
    if len(columns) > settings.db_max_columns:
        raise DatabaseActivityError(
            "Database result exceeds PLANTAIN_DB_MAX_COLUMNS; use a narrower projection"
        )
    if mode is DatabaseResultMode.VALUE and len(columns) != 1:
        raise DatabaseActivityError("resultMode value requires exactly one projected column")
    rows, bytes_truncated = _read_rows(
        result.mappings(),
        fetch_limit=fetch_limit,
        batch_size=settings.db_fetch_batch_size,
        max_result_bytes=settings.db_max_result_bytes,
    )
    if mode is not DatabaseResultMode.ROWS and bytes_truncated:
        raise DatabaseActivityError(
            "Single-result mode could not prove uniqueness within "
            "PLANTAIN_DB_MAX_RESULT_BYTES; "
            "use a narrower projection"
        )
    if mode is not DatabaseResultMode.ROWS and len(rows) > 1:
        raise DatabaseActivityError(
            f"resultMode {mode.value} returned more than one row; make the SQL deterministic"
        )
    row_truncated = mode is DatabaseResultMode.ROWS and len(rows) > max_rows
    if row_truncated:
        rows = rows[:max_rows]
    truncated = bytes_truncated or row_truncated
    chaining_value: Any
    if mode is DatabaseResultMode.VALUE:
        chaining_value = rows[0][columns[0]] if rows else None
    elif mode is DatabaseResultMode.ROW:
        chaining_value = rows[0] if rows else None
    else:
        chaining_value = rows
    query_result = QueryDatabaseResult(
        dialect=dialect,
        columns=columns,
        result=chaining_value,
        row_count=len(rows),
        truncated=truncated,
        result_mode=mode,
    )
    if (
        len(query_result.model_dump_json(by_alias=True).encode("utf-8"))
        > settings.db_max_result_bytes
    ):
        raise DatabaseActivityError(
            "Database result envelope exceeds PLANTAIN_DB_MAX_RESULT_BYTES; "
            "use a narrower projection or lower maxRows"
        )
    return query_result


def _statement(params: QueryDatabaseParams, project_root: Path) -> str:
    if params.sql is not None:
        return params.sql
    sql_root = (project_root / "sql").resolve()
    candidate = Path(str(params.file))
    if candidate.is_absolute() or candidate.suffix.casefold() != ".sql":
        raise DatabaseActivityError("Database SQL file must be a relative .sql path")
    if ".." in candidate.parts:
        raise DatabaseActivityError("Database SQL file escapes the sql directory")
    target = sql_root / candidate
    try:
        with open_binary_read_no_follow(target) as handle:
            payload = handle.read(MAX_SQL_FILE_BYTES + 1)
    except FileNotFoundError:
        raise DatabaseActivityError("Database SQL file is missing") from None
    except (OSError, AtomicPersistenceError) as exc:
        raise DatabaseActivityError("Database SQL file could not be read") from exc
    if len(payload) > MAX_SQL_FILE_BYTES:
        raise DatabaseActivityError("Database SQL file exceeds 1 MB")
    try:
        statement = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatabaseActivityError("Database SQL file must be readable UTF-8") from exc
    if not statement.strip():
        raise DatabaseActivityError("Database SQL file is empty")
    return statement


def _read_rows(
    mappings: Any,
    *,
    fetch_limit: int,
    batch_size: int,
    max_result_bytes: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    serialized_bytes = 2
    while len(rows) < fetch_limit:
        requested = min(batch_size, fetch_limit - len(rows))
        batch = mappings.fetchmany(requested)
        if not batch:
            return rows, False
        for raw in batch:
            separator_bytes = 1 if rows else 0
            available = max_result_bytes - serialized_bytes - separator_bytes
            byte_budget = [available]
            try:
                row = _portable(raw, byte_budget=byte_budget)
            except _CellLimitError:
                if not rows:
                    raise DatabaseActivityError(
                        "One database row exceeds PLANTAIN_DB_MAX_RESULT_BYTES; "
                        "use a narrower projection"
                    ) from None
                return rows, True
            if not isinstance(row, dict):
                raise DatabaseActivityError("Database row has an invalid mapping shape")
            row_bytes = available - byte_budget[0]
            rows.append(row)
            serialized_bytes += separator_bytes + row_bytes
            if len(rows) >= fetch_limit:
                break
    return rows, False


def _portable(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
    max_bytes: int | None = None,
    byte_budget: list[int] | None = None,
) -> Any:
    if depth > MAX_STRUCTURED_DEPTH:
        raise DatabaseActivityError("Database value exceeds the structured-depth limit")
    remaining = [MAX_STRUCTURED_ITEMS] if budget is None else budget
    remaining_bytes = (
        [MAX_BINARY_CELL_BYTES if max_bytes is None else max_bytes]
        if byte_budget is None
        else byte_budget
    )
    remaining[0] -= 1
    if remaining[0] < 0:
        raise DatabaseActivityError("Database value exceeds the structured-item limit")
    scalar = _portable_scalar(value)
    if scalar is not _UNHANDLED:
        _consume_json_scalar(scalar, remaining_bytes)
        return scalar
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _portable_binary(value, byte_budget=remaining_bytes)
    if isinstance(value, Mapping):
        return _portable_mapping(
            value,
            depth=depth,
            item_budget=remaining,
            byte_budget=remaining_bytes,
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _portable_sequence(
            value,
            depth=depth,
            item_budget=remaining,
            byte_budget=remaining_bytes,
        )
    if _is_oracle_lob(value):
        return _portable_lob(
            value,
            depth=depth,
            item_budget=remaining,
            byte_budget=remaining_bytes,
        )
    return _portable_fallback(value, byte_budget=remaining_bytes)


def _portable_mapping(
    value: Mapping[Any, Any],
    *,
    depth: int,
    item_budget: list[int],
    byte_budget: list[int],
) -> dict[str, Any]:
    _consume_bytes(2, byte_budget)
    result: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index:
            _consume_bytes(1, byte_budget)
        rendered_key = str(key)
        _consume_json_string(rendered_key, byte_budget)
        _consume_bytes(1, byte_budget)
        result[rendered_key] = _portable(
            item,
            depth=depth + 1,
            budget=item_budget,
            byte_budget=byte_budget,
        )
    return result


def _portable_sequence(
    value: Sequence[Any],
    *,
    depth: int,
    item_budget: list[int],
    byte_budget: list[int],
) -> list[Any]:
    _consume_bytes(2, byte_budget)
    result: list[Any] = []
    for index, item in enumerate(value):
        if index:
            _consume_bytes(1, byte_budget)
        result.append(
            _portable(
                item,
                depth=depth + 1,
                budget=item_budget,
                byte_budget=byte_budget,
            )
        )
    return result


def _portable_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return _UNHANDLED


def _portable_binary(
    value: bytes | bytearray | memoryview,
    *,
    byte_budget: list[int],
) -> dict[str, str]:
    size = value.nbytes if isinstance(value, memoryview) else len(value)
    encoded_bytes = 4 * ((size + 2) // 3)
    _consume_bytes(
        _BINARY_JSON_OVERHEAD_BYTES + encoded_bytes,
        byte_budget,
        message="Database binary value exceeds the cell limit; use a narrower projection",
    )
    binary = bytes(value)
    return {"$binaryBase64": base64.b64encode(binary).decode("ascii")}


def _portable_lob(
    value: Any,
    *,
    depth: int,
    item_budget: list[int],
    byte_budget: list[int],
) -> Any:
    try:
        size = int(value.size())
    except (TypeError, ValueError, OverflowError) as exc:
        raise DatabaseActivityError("Database LOB reported an invalid size") from exc
    if size < 0 or size > byte_budget[0]:
        _raise_cell_limit("Database LOB exceeds the cell limit; use a narrower projection")
    return _portable(
        value.read(),
        depth=depth + 1,
        budget=item_budget,
        byte_budget=byte_budget,
    )


def _portable_fallback(value: Any, *, byte_budget: list[int]) -> str:
    rendered = str(value)
    _consume_json_string(rendered, byte_budget)
    return rendered


def _consume_json_scalar(value: Any, byte_budget: list[int]) -> None:
    if isinstance(value, str):
        _consume_json_string(value, byte_budget)
        return
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        size = len(rendered.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DatabaseActivityError("Database value cannot be serialized safely") from exc
    _consume_bytes(size, byte_budget)


def _consume_json_string(value: str, byte_budget: list[int]) -> None:
    _consume_bytes(2, byte_budget)
    try:
        for offset in range(0, len(value), JSON_TEXT_CHUNK_CHARACTERS):
            chunk = value[offset : offset + JSON_TEXT_CHUNK_CHARACTERS]
            rendered = json.dumps(chunk, ensure_ascii=False)[1:-1]
            _consume_bytes(len(rendered.encode("utf-8")), byte_budget)
    except UnicodeError as exc:
        raise DatabaseActivityError("Database text value is not valid UTF-8") from exc


def _consume_bytes(
    size: int,
    byte_budget: list[int],
    *,
    message: str = "Database value exceeds the cell limit; use a narrower projection",
) -> None:
    if size > byte_budget[0]:
        _raise_cell_limit(message)
    byte_budget[0] -= size


def _raise_cell_limit(message: str) -> NoReturn:
    raise _CellLimitError(message)


def _is_oracle_lob(value: Any) -> bool:
    return (
        type(value).__module__.startswith("oracledb")
        and callable(getattr(value, "read", None))
        and callable(getattr(value, "size", None))
    )


def _sqlglot_dialect(dialect: str) -> str:
    return {"mssql": "tsql", "postgresql": "postgres"}.get(dialect, dialect)


__all__ = ["execute_query"]
