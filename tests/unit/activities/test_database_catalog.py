"""Server-bounded database catalog query coverage without live databases."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from plantain.activities.database.catalog import (
    catalog_object_matches,
    catalog_object_page,
    catalog_schema_matches,
    catalog_schema_page,
)
from plantain.activities.database.errors import DatabaseActivityError

PAGE_SIZE = 2


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        return self.rows


class _Connection:
    def __init__(
        self,
        dialect: Any,
        rows: list[dict[str, Any]],
        *,
        fail: bool = False,
    ) -> None:
        self.dialect = dialect
        self.rows = rows
        self.fail = fail
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if self.fail:
            raise SQLAlchemyError("synthetic catalog failure")
        return _Result(self.rows)


@pytest.mark.parametrize(
    "dialect",
    [postgresql.dialect(), mysql.dialect(), mssql.dialect(), oracle.dialect()],
    ids=["postgresql", "mysql", "mssql", "oracle"],
)
def test_schema_pages_use_bound_vendor_catalog_queries(dialect: Any) -> None:
    connection = _Connection(
        dialect,
        [{"name": "Beta"}, {"name": "Gamma"}, {"name": "Omega"}],
    )

    names = catalog_schema_page(
        cast("Connection", connection),
        after=("alpha", "Alpha"),
        page_size=PAGE_SIZE,
        include_system_schemas=False,
    )

    assert names == ["Beta", "Gamma", "Omega"]
    compiled = connection.statements[0].compile(dialect=dialect)
    rendered = str(compiled)
    expected_source = "all_users" if dialect.name == "oracle" else "information_schema"
    assert expected_source in rendered.lower()
    assert "Alpha" not in rendered
    parameter_values = list(compiled.params.values())
    assert "alpha" in parameter_values
    assert "Alpha" in parameter_values
    assert PAGE_SIZE + 1 in parameter_values


def test_object_catalog_queries_bind_scope_and_exact_filter() -> None:
    page_connection = _Connection(
        postgresql.dialect(),
        [{"name": "Orders", "type": "table"}, {"name": "OrdersView", "type": "view"}],
    )
    match_connection = _Connection(
        postgresql.dialect(),
        [{"name": "Orders", "type": "table"}],
    )

    page = catalog_object_page(
        cast("Connection", page_connection),
        "Brands",
        after=None,
        page_size=1,
        include_views=True,
    )
    matches = catalog_object_matches(
        cast("Connection", match_connection),
        "Brands",
        "orders",
        include_views=False,
    )

    assert page == [("Orders", "table"), ("OrdersView", "view")]
    assert matches == [("Orders", "table")]
    for connection in (page_connection, match_connection):
        statement = connection.statements[0]
        compiled = statement.compile(dialect=connection.dialect)
        assert "Brands" not in str(compiled)
        assert "Brands" in compiled.params.values()


def test_catalog_query_falls_back_for_unsupported_or_driver_failure() -> None:
    unsupported = _Connection(SimpleNamespace(name="sqlite"), [])
    failed = _Connection(postgresql.dialect(), [], fail=True)

    assert catalog_schema_matches(cast("Connection", unsupported), "main") is None
    assert unsupported.statements == []
    assert catalog_schema_matches(cast("Connection", failed), "main") is None
    assert len(failed.statements) == 1


def test_catalog_metadata_and_cursor_shapes_fail_closed() -> None:
    malformed = _Connection(postgresql.dialect(), [{"name": 7}])
    with pytest.raises(DatabaseActivityError, match="invalid schema catalog"):
        catalog_schema_matches(cast("Connection", malformed), "main")

    valid = _Connection(postgresql.dialect(), [])
    with pytest.raises(DatabaseActivityError, match="cursor does not match"):
        catalog_schema_page(
            cast("Connection", valid),
            after=("incomplete",),
            page_size=PAGE_SIZE,
            include_system_schemas=True,
        )
