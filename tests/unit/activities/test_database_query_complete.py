"""Complete bounded-query and portable-value coverage without an external database."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection

from plantain.activities.database import query as database_query
from plantain.activities.database.errors import DatabaseActivityError
from plantain.activities.database.query import (
    _is_oracle_lob,
    _portable,
    _read_rows,
    _sqlglot_dialect,
    _statement,
    execute_query,
)
from plantain.config import Settings
from plantain.models.database import DatabaseSource, QueryDatabaseParams

DUMMY_CREDENTIAL = "synthetic-query-credential"
EXPECTED_FLOAT = 3.5
EXPECTED_VALUE = 7
FETCH_BATCH_SIZE = 2
MAX_COLUMNS = 5
MAX_RESULT_BYTES = 16_384
MAX_ROWS = 10


class FakeMappings:
    """SQLAlchemy mappings facade with deterministic batching."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.offset = 0
        self.requests: list[int] = []

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        self.requests.append(size)
        batch = self.rows[self.offset : self.offset + size]
        self.offset += len(batch)
        return batch


class FakeResult:
    """Minimal result object used by execute_query."""

    def __init__(
        self,
        *,
        columns: list[str],
        rows: list[dict[str, Any]],
        returns_rows: bool = True,
    ) -> None:
        self.returns_rows = returns_rows
        self._columns = columns
        self._mappings = FakeMappings(rows)
        self.closed = False

    def keys(self) -> list[str]:
        return self._columns

    def mappings(self) -> FakeMappings:
        return self._mappings

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """Connection boundary that records statements without executing SQL."""

    def __init__(self, result: FakeResult, *, dialect: str = "postgresql") -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.result = result
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeResult:
        self.calls.append((statement, parameters))
        return self.result


def _source() -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "automation_reader",
            "password": DUMMY_CREDENTIAL,
            "dbUrl": "jdbc:postgresql://db.example.test/app",
        }
    )


def _params(sql: str | None = None, **overrides: Any) -> QueryDatabaseParams:
    payload: dict[str, Any] = {"id": "database_query", "source": _source()}
    if sql is not None:
        payload["sql"] = sql
    payload.update(overrides)
    return QueryDatabaseParams.model_validate(payload)


def _settings(root: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "project_root": root,
        "db_max_columns": MAX_COLUMNS,
        "db_max_rows": MAX_ROWS,
        "db_fetch_batch_size": FETCH_BATCH_SIZE,
        "db_max_result_bytes": MAX_RESULT_BYTES,
    }
    values.update(overrides)
    return cast("Settings", SimpleNamespace(**values))


def _execute(
    result: FakeResult,
    params: QueryDatabaseParams,
    settings: Settings,
    *,
    dialect: str = "postgresql",
) -> Any:
    connection = FakeConnection(result, dialect=dialect)
    return execute_query(cast("Connection", connection), params, settings)


def test_query_requires_a_row_set(tmp_path: Path) -> None:
    result = FakeResult(columns=[], rows=[], returns_rows=False)

    with pytest.raises(DatabaseActivityError, match="did not return a row set"):
        _execute(
            result,
            _params("SELECT 1"),
            _settings(tmp_path),
        )
    assert result.closed is True


def test_query_enforces_column_and_configured_row_caps(tmp_path: Path) -> None:
    with pytest.raises(DatabaseActivityError, match="MAX_COLUMNS"):
        _execute(
            FakeResult(columns=["a", "b"], rows=[]),
            _params("SELECT 1"),
            _settings(tmp_path, db_max_columns=1),
        )
    with pytest.raises(DatabaseActivityError, match="maxRows cannot exceed"):
        _execute(
            FakeResult(columns=["value"], rows=[]),
            _params("SELECT 1", maxRows=2),
            _settings(tmp_path, db_max_rows=1),
        )


def test_single_result_rejects_byte_truncation_before_claiming_uniqueness(
    tmp_path: Path,
) -> None:
    first = {"value": "short"}
    second = {"value": "x" * 100}
    one_row_budget = len(b"[]") + len(b'{"value":"short"}') + 1

    with pytest.raises(DatabaseActivityError, match="could not prove uniqueness"):
        _execute(
            FakeResult(columns=["value"], rows=[first, second]),
            _params("SELECT value FROM items", resultMode="row"),
            _settings(tmp_path, db_max_result_bytes=one_row_budget),
        )


def test_result_envelope_is_checked_after_row_streaming(tmp_path: Path) -> None:
    with pytest.raises(DatabaseActivityError, match="result envelope exceeds"):
        _execute(
            FakeResult(columns=["value"], rows=[{"value": "ok"}]),
            _params("SELECT value FROM items", resultMode="row"),
            _settings(tmp_path, db_max_result_bytes=40),
        )


def test_query_uses_dialect_name_and_executes_resolved_binds(tmp_path: Path) -> None:
    result = FakeResult(columns=["value"], rows=[{"value": EXPECTED_VALUE}])
    connection = FakeConnection(result, dialect="mssql")

    actual = execute_query(
        cast("Connection", connection),
        _params(
            "SELECT :value AS value",
            parameters={"value": EXPECTED_VALUE},
            resultMode="value",
        ),
        _settings(tmp_path),
    )

    assert actual.dialect == "mssql"
    assert actual.result == EXPECTED_VALUE
    assert len(connection.calls) == 1
    assert "TOP 2" in str(connection.calls[0][0])
    assert ":value" in str(connection.calls[0][0])
    assert connection.calls[0][0].get_execution_options()["yield_per"] == FETCH_BATCH_SIZE
    assert connection.calls[0][1] == {"value": EXPECTED_VALUE}
    assert result.closed is True


def test_sql_file_must_be_relative_sql_path(tmp_path: Path) -> None:
    with pytest.raises(DatabaseActivityError, match=r"relative \.sql path"):
        _statement(_params(file=str(tmp_path / "query.sql")), tmp_path)
    with pytest.raises(DatabaseActivityError, match=r"relative \.sql path"):
        _statement(_params(file="query.txt"), tmp_path)


def test_sql_file_must_exist_and_read_successfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(DatabaseActivityError, match="is missing"):
        _statement(_params(file="missing.sql"), tmp_path)

    sql_root = tmp_path / "sql"
    sql_root.mkdir()
    target = sql_root / "lookup.sql"
    target.write_text("SELECT 1", encoding="utf-8")

    def reject_open(_path: Path) -> Any:
        raise OSError("synthetic filesystem detail")

    monkeypatch.setattr(
        "plantain.activities.database.query.open_binary_read_no_follow",
        reject_open,
    )
    with pytest.raises(DatabaseActivityError, match="could not be read") as captured:
        _statement(_params(file="lookup.sql"), tmp_path)
    assert "synthetic filesystem detail" not in str(captured.value)


def test_sql_file_rejects_symlinked_path(tmp_path: Path) -> None:
    sql_root = tmp_path / "sql"
    sql_root.mkdir()
    outside = tmp_path / "outside.sql"
    outside.write_text("SELECT private_value FROM secrets", encoding="utf-8")
    (sql_root / "lookup.sql").symlink_to(outside)

    with pytest.raises(DatabaseActivityError, match="could not be read"):
        _statement(_params(file="lookup.sql"), tmp_path)

    assert outside.read_text(encoding="utf-8") == "SELECT private_value FROM secrets"


def test_read_rows_batches_and_marks_later_byte_overflow() -> None:
    mappings = FakeMappings(
        [
            {"value": "first"},
            {"value": "second"},
            {"value": "x" * 100},
        ]
    )
    first_two_bytes = len(b'[{"value":"first"},{"value":"second"}]')

    rows, truncated = _read_rows(
        mappings,
        fetch_limit=3,
        batch_size=2,
        max_result_bytes=first_two_bytes,
    )

    assert rows == [{"value": "first"}, {"value": "second"}]
    assert truncated is True
    assert mappings.requests == [2, 1]


def test_read_rows_rejects_one_oversized_row() -> None:
    with pytest.raises(DatabaseActivityError, match="One database row exceeds"):
        _read_rows(
            FakeMappings([{"value": "too-large"}]),
            fetch_limit=1,
            batch_size=1,
            max_result_bytes=2,
        )


def test_read_rows_uses_configured_budget_for_binary_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_query, "MAX_BINARY_CELL_BYTES", 2)

    rows, truncated = _read_rows(
        FakeMappings([{"value": b"abc"}]),
        fetch_limit=1,
        batch_size=1,
        max_result_bytes=MAX_RESULT_BYTES,
    )

    assert rows == [{"value": {"$binaryBase64": "YWJj"}}]
    assert truncated is False


def test_portable_scalar_and_nested_values() -> None:
    identifier = UUID("12345678-1234-5678-1234-567812345678")

    assert _portable(None) is None
    assert _portable(True) is True
    assert _portable(EXPECTED_FLOAT) == EXPECTED_FLOAT
    assert _portable(time(12, 30, 15)) == "12:30:15"
    assert _portable(identifier) == str(identifier)
    assert _portable(bytearray(b"abc")) == {"$binaryBase64": "YWJj"}
    assert _portable(memoryview(b"xyz")) == {"$binaryBase64": "eHl6"}
    assert _portable({7: ["value", {"nested": 2}]}) == {"7": ["value", {"nested": 2}]}


def test_portable_values_enforce_depth_item_and_binary_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(DatabaseActivityError, match="structured-depth"):
        _portable("value", depth=database_query.MAX_STRUCTURED_DEPTH + 1)
    with pytest.raises(DatabaseActivityError, match="structured-item"):
        _portable("value", budget=[0])

    monkeypatch.setattr(database_query, "MAX_BINARY_CELL_BYTES", 2)
    with pytest.raises(DatabaseActivityError, match="binary value exceeds"):
        _portable(b"abc")
    with pytest.raises(DatabaseActivityError, match="value exceeds the cell limit"):
        _portable(SimpleNamespace(value="long-rendered-value"))


def test_portable_enforces_combined_cell_budget_before_binary_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(DatabaseActivityError, match="value exceeds the cell limit"):
        _portable({"first": "1234", "second": "5678"}, max_bytes=16)

    def unexpected_encode(_value: bytes) -> bytes:
        raise AssertionError("oversized binary must be rejected before encoding")

    monkeypatch.setattr(database_query.base64, "b64encode", unexpected_encode)
    with pytest.raises(DatabaseActivityError, match="binary value exceeds"):
        _portable(memoryview(b"abc"), max_bytes=2)


def test_oracle_lob_is_bounded_and_converted() -> None:
    def read_lob(value: Any) -> bytes:
        value.read_calls += 1
        return value.payload

    oracle_lob_type = type(
        "LOB",
        (),
        {
            "__module__": "oracledb.lob",
            "size": lambda self: len(self.payload),
            "read": read_lob,
        },
    )
    small = oracle_lob_type()
    small.payload = b"abc"
    small.read_calls = 0
    large = oracle_lob_type()
    large.payload = b"x" * (database_query.MAX_BINARY_CELL_BYTES + 1)
    large.read_calls = 0

    assert _is_oracle_lob(small) is True
    assert _portable(small) == {"$binaryBase64": "YWJj"}
    assert small.read_calls == 1
    with pytest.raises(DatabaseActivityError, match="LOB exceeds"):
        _portable(large)
    assert large.read_calls == 0


def test_non_oracle_lob_shape_uses_safe_fallback() -> None:
    value = SimpleNamespace(read=lambda: b"value", size=lambda: 5)

    assert _is_oracle_lob(value) is False
    assert _portable(value).startswith("namespace(")


@pytest.mark.parametrize(
    "dialect, expected",
    [("mssql", "tsql"), ("postgresql", "postgres"), ("mysql", "mysql")],
)
def test_sqlglot_dialect_mapping(dialect: str, expected: str) -> None:
    assert _sqlglot_dialect(dialect) == expected
