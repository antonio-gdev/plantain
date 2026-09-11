"""Database activity exposure boundary tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.database.activities import (
    discover_database,
    query_database,
    register_database_activities,
    verify_database_result,
)
from plantain.activities.database.errors import DatabaseActivityError
from plantain.engine.registry import ActivityRegistry
from plantain.models.database import (
    DatabaseDiscoveryResult,
    DatabaseObject,
    DatabaseResultMode,
    DiscoverDatabaseParams,
    DiscoveryPhase,
    QueryDatabaseParams,
    QueryDatabaseResult,
    VerifyDatabaseResultParams,
)
from plantain.security.secrets import SecretRegistry

DB_CREDENTIAL_VALUE = "database-password-must-not-leak"
DB_URL = "jdbc:postgresql://db.internal.test:5432/clothing"


class FakeDatabase:
    def __init__(self) -> None:
        self.discovery_result = DatabaseDiscoveryResult(
            phase=DiscoveryPhase.TABLES,
            dialect="postgresql",
            schema_name="Brands",
            objects=[DatabaseObject(name="Uno", type="table")],
            item_count=1,
        )
        self.query_result = QueryDatabaseResult(
            dialect="postgresql",
            columns=["Item", "Price"],
            result={"Item": "Jacket", "Price": "29.99"},
            row_count=1,
            result_mode=DatabaseResultMode.ROW,
        )
        self.fail_query = False

    async def discover(self, _params: DiscoverDatabaseParams) -> DatabaseDiscoveryResult:
        return self.discovery_result

    async def query(self, _params: QueryDatabaseParams) -> QueryDatabaseResult:
        if self.fail_query:
            raise DatabaseActivityError("synthetic query failure")
        return self.query_result


def _context(database: FakeDatabase) -> SimpleNamespace:
    async def resolve_database() -> FakeDatabase:
        return database

    context = SimpleNamespace(
        services=SimpleNamespace(database=resolve_database),
        scenario=SimpleNamespace(scenario="Database unit test"),
        secrets=SecretRegistry(),
        current_activity="databaseActivity",
        current_step_id="database_step",
        operations=[],
    )

    def add_operation(**operation: Any) -> None:
        context.operations.append(operation)

    context.add_operation = add_operation
    return context


def _source() -> dict[str, str]:
    return {
        "username": "database-user",
        "password": DB_CREDENTIAL_VALUE,
        "dbUrl": DB_URL,
    }


def test_registers_only_agent_safe_read_only_database_activities() -> None:
    registry = ActivityRegistry()

    register_database_activities(registry)

    assert set(registry.names()) == {
        "discoverDatabase",
        "queryDatabase",
        "verifyDatabaseResult",
    }
    assert "db-mutate" not in registry.names()
    assert "executeHumanMutation" not in registry.names()


def test_discovery_trace_contains_scope_and_metadata_without_credentials() -> None:
    database = FakeDatabase()
    context = _context(database)
    params = DiscoverDatabaseParams.model_validate(
        {
            "id": "discover_brands",
            "source": _source(),
            "phase": "tables",
            "schema": "Brands",
        }
    )

    result = asyncio.run(discover_database(cast("Any", context), params))

    assert result.objects[0].name == "Uno"
    operation = context.operations[0]
    assert operation["phase"] == "discovery"
    assert operation["target"] == "phase=tables schema=Brands"
    assert operation["operation_actual"]["itemCount"] == 1
    rendered = json.dumps(operation)
    assert DB_CREDENTIAL_VALUE not in rendered
    assert DB_URL not in rendered


def test_query_trace_is_bounded_and_keeps_the_chainable_result() -> None:
    database = FakeDatabase()
    context = _context(database)
    statement = (
        'SELECT "Item", "Price" FROM "Brands"."Uno" '
        'WHERE "Color" = :color ORDER BY "Date" DESC LIMIT 1'
    )
    params = QueryDatabaseParams.model_validate(
        {
            "id": "latest_item",
            "source": _source(),
            "sql": statement,
            "parameters": {"color": "blue"},
            "resultMode": "row",
        }
    )

    result = asyncio.run(query_database(cast("Any", context), params))

    assert result.result == {"Item": "Jacket", "Price": "29.99"}
    operation = context.operations[0]
    assert operation["operation_type"] == "SELECT"
    assert operation["target"].startswith("inline-select sha256=")
    assert operation["operation_input"]["parameters"] == {"color": "blue"}
    assert operation["operation_actual"]["result"] == result.result
    rendered = json.dumps(operation)
    assert statement not in rendered
    assert DB_CREDENTIAL_VALUE not in rendered
    assert DB_URL not in rendered


def test_verification_trace_retains_sanitized_expected_and_actual_values() -> None:
    context = _context(FakeDatabase())
    params = VerifyDatabaseResultParams.model_validate(
        {
            "id": "verify_latest_item",
            "actual": {"Item": "Jacket", "Price": "29.99", "Color": "blue"},
            "expected": {"Item": "Jacket", "Price": "29.99"},
            "matchMode": "contains",
        }
    )

    result = asyncio.run(verify_database_result(cast("Any", context), params))

    assert result.matched_rows == 1
    operation = context.operations[0]
    assert operation["phase"] == "verification"
    assert operation["operation_input"]["Color"] == "blue"
    assert operation["operation_expected"] == {
        "Item": "Jacket",
        "Price": "29.99",
    }
    assert operation["operation_actual"]["matchedRows"] == 1


def test_failed_query_records_only_safe_failure_classification() -> None:
    database = FakeDatabase()
    database.fail_query = True
    context = _context(database)
    params = QueryDatabaseParams.model_validate(
        {
            "id": "failed_query",
            "source": _source(),
            "sql": "SELECT value FROM safe_table",
            "resultMode": "rows",
        }
    )

    with pytest.raises(DatabaseActivityError, match="synthetic query failure"):
        asyncio.run(query_database(cast("Any", context), params))

    operation = context.operations[0]
    assert operation["status"] == "failed"
    assert operation["error_type"] == "DatabaseActivityError"
    rendered = json.dumps(operation)
    assert "synthetic query failure" not in rendered
    assert DB_CREDENTIAL_VALUE not in rendered
    assert DB_URL not in rendered
