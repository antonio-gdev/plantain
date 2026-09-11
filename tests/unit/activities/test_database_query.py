"""Bounded query execution and result-shaping tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from plantain.activities.database.errors import DatabaseActivityError
from plantain.activities.database.query import _portable, execute_query
from plantain.config import Settings
from plantain.models.database import (
    DatabaseResultMode,
    DatabaseSource,
    QueryDatabaseParams,
)

EXPECTED_PRICE = 19.99
EXPECTED_ROW_COUNT = 2
MAX_TEST_SQL_FILE_BYTES = 1_000_000


@pytest.fixture
def connection() -> Iterator[Connection]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as active:
        yield active
    engine.dispose(close=True)


def _source() -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "reader",
            "password": "unit-test-password",
            "dbUrl": "jdbc:postgresql://db.example.test/app",
        }
    )


def _params(sql: str | None = None, **overrides: Any) -> QueryDatabaseParams:
    payload: dict[str, Any] = {
        "id": "database_query",
        "source": _source(),
    }
    if sql is not None:
        payload["sql"] = sql
    payload.update(overrides)
    return QueryDatabaseParams.model_validate(payload)


def _settings(root: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "project_root": root,
        "db_max_columns": 20,
        "db_max_rows": 10,
        "db_fetch_batch_size": 2,
        "db_max_result_bytes": 16_384,
    }
    values.update(overrides)
    return cast("Settings", SimpleNamespace(**values))


def test_result_modes_return_scalar_row_or_rows(
    connection: Connection,
    tmp_path: Path,
) -> None:
    value = execute_query(
        connection,
        _params(
            "SELECT :price AS Price",
            parameters={"price": 19.99},
            resultMode="value",
        ),
        _settings(tmp_path),
    )
    row = execute_query(
        connection,
        _params(
            "SELECT :item AS Item, :price AS Price",
            parameters={"item": "Jacket", "price": 19.99},
            resultMode="row",
        ),
        _settings(tmp_path),
    )
    rows = execute_query(
        connection,
        _params("SELECT 1 AS Item UNION ALL SELECT 2 AS Item", resultMode="rows"),
        _settings(tmp_path),
    )

    assert value.result == EXPECTED_PRICE
    assert value.result_mode is DatabaseResultMode.VALUE
    assert value.row_count == 1
    assert row.result == {"Item": "Jacket", "Price": 19.99}
    assert row.result_mode is DatabaseResultMode.ROW
    assert rows.result == [{"Item": 1}, {"Item": 2}]
    assert rows.result_mode is DatabaseResultMode.ROWS


def test_value_mode_distinguishes_no_row_from_sql_null_using_row_count(
    connection: Connection,
    tmp_path: Path,
) -> None:
    no_row = execute_query(
        connection,
        _params("SELECT 1 AS Value WHERE 0", resultMode="value"),
        _settings(tmp_path),
    )
    sql_null = execute_query(
        connection,
        _params("SELECT NULL AS Value", resultMode="value"),
        _settings(tmp_path),
    )

    assert no_row.result is None
    assert no_row.row_count == 0
    assert sql_null.result is None
    assert sql_null.row_count == 1


def test_legacy_single_result_maps_to_row_and_conflicts_fail_validation() -> None:
    legacy = _params("SELECT 1 AS Value", singleResult=True)

    assert legacy.effective_result_mode is DatabaseResultMode.ROW
    with pytest.raises(ValidationError, match="cannot combine"):
        _params(
            "SELECT 1 AS Value",
            singleResult=True,
            resultMode="value",
        )
    with pytest.raises(ValidationError, match="maxRows"):
        _params("SELECT 1 AS Value", resultMode="row", maxRows=1)


def test_single_modes_require_deterministic_shape(
    connection: Connection,
    tmp_path: Path,
) -> None:
    with pytest.raises(DatabaseActivityError, match="exactly one projected column"):
        execute_query(
            connection,
            _params("SELECT 1 AS First, 2 AS Second", resultMode="value"),
            _settings(tmp_path),
        )
    with pytest.raises(DatabaseActivityError, match="more than one row"):
        execute_query(
            connection,
            _params(
                "SELECT 1 AS Value UNION ALL SELECT 2 AS Value",
                resultMode="row",
            ),
            _settings(tmp_path),
        )


def test_rows_mode_is_streamed_and_explicitly_marks_truncation(
    connection: Connection,
    tmp_path: Path,
) -> None:
    result = execute_query(
        connection,
        _params(
            "SELECT 1 AS Value UNION ALL SELECT 2 AS Value UNION ALL SELECT 3 AS Value",
            resultMode="rows",
            maxRows=2,
        ),
        _settings(tmp_path),
    )

    assert result.result == [{"Value": 1}, {"Value": 2}]
    assert result.row_count == EXPECTED_ROW_COUNT
    assert result.truncated is True


def test_bind_names_must_match_exactly_and_mutation_never_executes(
    connection: Connection,
    tmp_path: Path,
) -> None:
    with pytest.raises(DatabaseActivityError, match=r"missing=1, unexpected=1"):
        execute_query(
            connection,
            _params(
                "SELECT :required AS Value",
                parameters={"unexpected": 1},
            ),
            _settings(tmp_path),
        )
    with pytest.raises(RuntimeError, match="SELECT statements only"):
        execute_query(
            connection,
            _params("DELETE FROM uno"),
            _settings(tmp_path),
        )


def test_sql_files_are_utf8_bounded_and_contained(
    connection: Connection,
    tmp_path: Path,
) -> None:
    sql_root = tmp_path / "sql"
    sql_root.mkdir()
    (sql_root / "lookup.sql").write_text("SELECT :value AS Value", encoding="utf-8")
    result = execute_query(
        connection,
        _params(file="lookup.sql", parameters={"value": "found"}, resultMode="value"),
        _settings(tmp_path),
    )

    assert result.result == "found"
    (sql_root / "empty.sql").write_text("  ", encoding="utf-8")
    (sql_root / "invalid.sql").write_bytes(b"\xff")
    (sql_root / "oversized.sql").write_bytes(b"x" * (MAX_TEST_SQL_FILE_BYTES + 1))
    with pytest.raises(DatabaseActivityError, match="escapes"):
        execute_query(
            connection,
            _params(file="../escape.sql"),
            _settings(tmp_path),
        )
    with pytest.raises(DatabaseActivityError, match="is empty"):
        execute_query(connection, _params(file="empty.sql"), _settings(tmp_path))
    with pytest.raises(DatabaseActivityError, match="readable UTF-8"):
        execute_query(connection, _params(file="invalid.sql"), _settings(tmp_path))
    with pytest.raises(DatabaseActivityError, match="exceeds 1 MB"):
        execute_query(connection, _params(file="oversized.sql"), _settings(tmp_path))


def test_result_size_and_portable_cell_limits_fail_closed(
    connection: Connection,
    tmp_path: Path,
) -> None:
    with pytest.raises(DatabaseActivityError, match="exceeds"):
        execute_query(
            connection,
            _params(
                "SELECT :value AS Value",
                parameters={"value": "x" * 2_000},
                resultMode="value",
            ),
            _settings(tmp_path, db_max_result_bytes=1_024),
        )

    assert _portable(Decimal("19.990")) == "19.990"
    assert _portable(date(2026, 8, 23)) == "2026-08-23"
    assert _portable(datetime(2026, 8, 23, tzinfo=UTC)) == "2026-08-23T00:00:00+00:00"
    assert _portable(b"abc") == {"$binaryBase64": "YWJj"}
