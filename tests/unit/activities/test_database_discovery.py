"""Scoped Inspector-only database discovery tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.engine import Connection

import plantain.activities.database.discovery as discovery_module
from plantain.activities.database.discovery import discover_metadata
from plantain.activities.database.errors import DatabaseActivityError
from plantain.models.database import (
    DatabaseSource,
    DiscoverDatabaseParams,
    DiscoveryPhase,
)


class FakeInspector:
    default_schema_name = "Brands"

    def get_schema_names(self) -> list[str]:
        return ["public", "pg_catalog", "Brands"]

    def get_table_names(self, *, schema: str | None = None) -> list[str]:
        assert schema == "Brands"
        return ["Uno", "Dos"]

    def get_view_names(self, *, schema: str | None = None) -> list[str]:
        assert schema == "Brands"
        return ["Tres"]

    def get_pk_constraint(
        self,
        table_name: str,
        *,
        schema: str | None = None,
    ) -> dict[str, Any]:
        assert (schema, table_name) == ("Brands", "Uno")
        return {"name": "pk_uno", "constrained_columns": ["Item"]}

    def get_columns(
        self,
        table_name: str,
        *,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        assert (schema, table_name) == ("Brands", "Uno")
        return [
            {
                "name": "Item",
                "type": "VARCHAR(100)",
                "nullable": False,
                "default": None,
                "autoincrement": False,
                "comment": "Released item",
            },
            {
                "name": "Price",
                "type": "NUMERIC(10, 2)",
                "nullable": False,
                "default": "0",
                "autoincrement": False,
            },
            {
                "name": "Date",
                "type": "DATE",
                "nullable": False,
                "autoincrement": False,
            },
        ]

    def get_table_comment(
        self,
        table_name: str,
        *,
        schema: str | None = None,
    ) -> dict[str, Any]:
        assert (schema, table_name) == ("Brands", "Uno")
        return {"text": "Clothing releases"}

    def get_foreign_keys(
        self,
        table_name: str,
        *,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        assert (schema, table_name) == ("Brands", "Uno")
        return [
            {
                "name": "fk_uno_brand",
                "constrained_columns": ["Item"],
                "referred_schema": "catalog",
                "referred_table": "items",
                "referred_columns": ["name"],
                "options": {},
            }
        ]

    def get_indexes(
        self,
        table_name: str,
        *,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        assert (schema, table_name) == ("Brands", "Uno")
        return [
            {
                "name": "idx_uno_date",
                "column_names": ["Date"],
                "unique": False,
                "expressions": [],
            }
        ]

    def get_unique_constraints(
        self,
        table_name: str,
        *,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        assert (schema, table_name) == ("Brands", "Uno")
        return [{"name": "uq_uno_item", "column_names": ["Item"]}]

    def get_check_constraints(
        self,
        table_name: str,
        *,
        schema: str | None = None,
    ) -> list[dict[str, Any]]:
        assert (schema, table_name) == ("Brands", "Uno")
        return [{"name": "ck_uno_price", "sqltext": "Price >= 0"}]


def _source() -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "reader",
            "password": "unit-test-password",
            "dbUrl": "jdbc:postgresql://db.example.test/clothing",
        }
    )


def _params(**values: Any) -> DiscoverDatabaseParams:
    return DiscoverDatabaseParams.model_validate(
        {"id": "discover_database", "source": _source(), **values}
    )


@pytest.fixture(autouse=True)
def fake_inspector(monkeypatch: pytest.MonkeyPatch) -> None:
    inspector = FakeInspector()
    monkeypatch.setattr(discovery_module, "inspect", lambda _connection: inspector)


def _connection() -> Connection:
    return cast("Connection", SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))


def test_schema_discovery_filters_system_names_and_uses_opaque_pagination() -> None:
    first = discover_metadata(
        _connection(),
        _params(phase="schemas", pageSize=1),
        max_result_bytes=16_384,
    )
    second = discover_metadata(
        _connection(),
        _params(phase="schemas", pageSize=1, cursor=first.next_cursor),
        max_result_bytes=16_384,
    )

    assert first.phase is DiscoveryPhase.SCHEMAS
    assert first.schemas == ["Brands"]
    assert first.has_more is True
    assert first.next_cursor is not None
    assert "Brands" not in first.next_cursor
    assert second.schemas == ["public"]
    assert second.has_more is False


def test_table_discovery_resolves_case_and_includes_views_on_request() -> None:
    result = discover_metadata(
        _connection(),
        _params(phase="tables", schema="brands", includeViews=True),
        max_result_bytes=16_384,
    )

    assert result.schema_name == "Brands"
    assert [(item.name, item.type) for item in result.objects] == [
        ("Dos", "table"),
        ("Tres", "view"),
        ("Uno", "table"),
    ]


def test_selected_table_returns_complete_structure_without_application_rows() -> None:
    result = discover_metadata(
        _connection(),
        _params(phase="table", schema="brands", table="uno", includeViews=True),
        max_result_bytes=16_384,
    )

    metadata = result.table_metadata
    assert metadata is not None
    assert metadata.name == "Uno"
    assert metadata.comment == "Clothing releases"
    assert [column.name for column in metadata.columns] == ["Item", "Price", "Date"]
    assert metadata.columns[0].primary_key is True
    assert metadata.primary_key.columns == ["Item"]
    assert metadata.foreign_keys[0].referred_table == "items"
    assert metadata.indexes[0].columns == ["Date"]
    assert metadata.unique_constraints[0].columns == ["Item"]
    assert metadata.check_constraints[0].sql_text == "Price >= 0"


def test_cursor_scope_and_metadata_size_fail_closed() -> None:
    schemas = discover_metadata(
        _connection(),
        _params(phase="schemas", pageSize=1),
        max_result_bytes=16_384,
    )
    with pytest.raises(DatabaseActivityError, match="does not match"):
        discover_metadata(
            _connection(),
            _params(
                phase="tables",
                schema="Brands",
                cursor=schemas.next_cursor,
            ),
            max_result_bytes=16_384,
        )
    with pytest.raises(DatabaseActivityError, match="exceeds"):
        discover_metadata(
            _connection(),
            _params(phase="table", schema="Brands", table="Uno"),
            max_result_bytes=100,
        )
