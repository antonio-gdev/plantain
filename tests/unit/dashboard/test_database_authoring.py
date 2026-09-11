"""Progressive dashboard database authoring remains evidence-grounded and read-only."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from plantain.dashboard.agent.database_authoring import (
    DATABASE_QUERY_OUTPUT,
    DATABASE_VERIFICATION_OUTPUT,
    DatabaseDraftValidationError,
    database_authoring_context,
    validate_database_draft,
)
from plantain.dashboard.agent.database_models import (
    DATABASE_DISCOVERY_OUTPUT,
    DatabaseDiscoveryRequest,
    DatabaseRunEvidence,
    DatabaseSourceReferences,
)
from plantain.models.database import (
    DatabaseColumn,
    DatabaseDiscoveryResult,
    DatabaseObject,
    DatabaseTableMetadata,
    DiscoveryPhase,
)
from plantain.models.scenario import ScenarioDefinition, StepDefinition

RUN_ID = "a" * 32


def _reference(name: str) -> str:
    return f"env:{name}"


def _references() -> DatabaseSourceReferences:
    return DatabaseSourceReferences(
        username=_reference("APP_DB_USERNAME"),
        password=_reference("APP_DB_PASSWORD"),
        db_url=_reference("APP_DB_URL"),
    )


def _source() -> dict[str, str]:
    return _references().model_dump(mode="json", by_alias=True)


def _evidence(
    request: DatabaseDiscoveryRequest,
    discovery: DatabaseDiscoveryResult,
) -> DatabaseRunEvidence:
    return DatabaseRunEvidence(
        run_id=RUN_ID,
        scenario="Database discovery",
        source_path="generated/database/discovery.yaml",
        source=_references(),
        request=request,
        discovery=discovery,
    )


def _scenario(
    *steps: StepDefinition,
    outputs: Mapping[str, str],
) -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario="Generated database workflow",
        steps=list(steps),
        outputs=dict(outputs),
    )


def _discover_step(
    phase: DiscoveryPhase,
    *,
    schema: str | None = None,
    table: str | None = None,
    cursor: str | None = None,
    include_views: bool = False,
    page_size: int = 250,
) -> StepDefinition:
    params: dict[str, object] = {
        "id": "discover",
        "source": _source(),
        "phase": phase.value,
        "includeViews": include_views,
        "pageSize": page_size,
    }
    if schema is not None:
        params["schema"] = schema
    if table is not None:
        params["table"] = table
    if cursor is not None:
        params["cursor"] = cursor
    return StepDefinition(activity="discoverDatabase", params=params)


def _query_step(
    statement: str,
    *,
    parameters: Mapping[str, object] | None = None,
) -> StepDefinition:
    return StepDefinition(
        activity="queryDatabase",
        params={
            "id": "query",
            "source": _source(),
            "sql": statement,
            "parameters": dict(parameters or {}),
            "resultMode": "rows",
            "maxRows": 100,
        },
    )


def _table_evidence(
    *,
    dialect: str = "postgresql",
    schema: str = "public",
    table: str = "customers",
    column: str = "id",
) -> DatabaseRunEvidence:
    return _evidence(
        DatabaseDiscoveryRequest(
            phase=DiscoveryPhase.TABLE,
            schema_name=schema,
            table=table,
        ),
        DatabaseDiscoveryResult(
            phase=DiscoveryPhase.TABLE,
            dialect=dialect,
            schema_name=schema,
            table_metadata=DatabaseTableMetadata(
                name=table,
                type="table",
                columns=[
                    DatabaseColumn(
                        name=column,
                        type="INTEGER",
                        nullable=False,
                        ordinal_position=1,
                    )
                ],
            ),
            item_count=1,
        ),
    )


def test_initial_workflow_is_metadata_only_and_uses_environment_references() -> None:
    initial = _scenario(
        _discover_step(DiscoveryPhase.SCHEMAS),
        outputs={DATABASE_DISCOVERY_OUTPUT: "${discover}"},
    )

    validate_database_draft(initial, None)

    direct_query = _scenario(
        _query_step('SELECT "id" FROM "public"."customers"'),
        outputs={DATABASE_QUERY_OUTPUT: "${query}"},
    )
    with pytest.raises(DatabaseDraftValidationError, match="exactly one discoverDatabase"):
        validate_database_draft(direct_query, None)


def test_schema_page_can_select_current_item_or_preserve_exact_pagination() -> None:
    evidence = _evidence(
        DatabaseDiscoveryRequest(
            phase=DiscoveryPhase.SCHEMAS,
            page_size=25,
        ),
        DatabaseDiscoveryResult(
            phase=DiscoveryPhase.SCHEMAS,
            dialect="postgresql",
            schemas=["audit", "public"],
            item_count=2,
            has_more=True,
            next_cursor="next-schema-page",
        ),
    )
    selection = _scenario(
        _discover_step(
            DiscoveryPhase.TABLES,
            schema="public",
            include_views=True,
        ),
        outputs={DATABASE_DISCOVERY_OUTPUT: "${discover}"},
    )
    continuation = _scenario(
        _discover_step(
            DiscoveryPhase.SCHEMAS,
            cursor="next-schema-page",
            page_size=25,
        ),
        outputs={DATABASE_DISCOVERY_OUTPUT: "${discover}"},
    )

    validate_database_draft(selection, evidence)
    validate_database_draft(continuation, evidence)

    changed_scope = _scenario(
        _discover_step(
            DiscoveryPhase.SCHEMAS,
            cursor="next-schema-page",
            page_size=50,
        ),
        outputs={DATABASE_DISCOVERY_OUTPUT: "${discover}"},
    )
    with pytest.raises(DatabaseDraftValidationError, match="request scope"):
        validate_database_draft(changed_scope, evidence)


def test_object_page_allows_only_returned_tables_and_opted_in_views() -> None:
    evidence = _evidence(
        DatabaseDiscoveryRequest(
            phase=DiscoveryPhase.TABLES,
            schema_name="public",
            include_views=True,
        ),
        DatabaseDiscoveryResult(
            phase=DiscoveryPhase.TABLES,
            dialect="postgresql",
            schema_name="public",
            objects=[
                DatabaseObject(name="customers", type="table"),
                DatabaseObject(name="active_customers", type="view"),
            ],
            item_count=2,
        ),
    )
    selected_view = _scenario(
        _discover_step(
            DiscoveryPhase.TABLE,
            schema="public",
            table="active_customers",
            include_views=True,
        ),
        outputs={DATABASE_DISCOVERY_OUTPUT: "${discover}"},
    )

    validate_database_draft(selected_view, evidence)

    view_without_opt_in = _scenario(
        _discover_step(
            DiscoveryPhase.TABLE,
            schema="public",
            table="active_customers",
        ),
        outputs={DATABASE_DISCOVERY_OUTPUT: "${discover}"},
    )
    with pytest.raises(DatabaseDraftValidationError, match="requires includeViews"):
        validate_database_draft(view_without_opt_in, evidence)


@pytest.mark.parametrize(
    ("dialect", "schema", "table", "column", "statement"),
    [
        (
            "postgresql",
            "public",
            "customers",
            "id",
            'SELECT "id" FROM "public"."customers" WHERE "id" = :value',
        ),
        (
            "mysql",
            "app",
            "customers",
            "id",
            "SELECT `id` FROM `app`.`customers` WHERE `id` = :value",
        ),
        (
            "mssql",
            "dbo",
            "Customers",
            "Id",
            "SELECT [Id] FROM [dbo].[Customers] WHERE [Id] = :value",
        ),
        (
            "oracle",
            "APP",
            "CUSTOMERS",
            "ID",
            'SELECT "ID" FROM "APP"."CUSTOMERS" WHERE "ID" = :value',
        ),
    ],
)
def test_reflected_query_is_validated_for_each_supported_dialect(
    dialect: str,
    schema: str,
    table: str,
    column: str,
    statement: str,
) -> None:
    evidence = _table_evidence(
        dialect=dialect,
        schema=schema,
        table=table,
        column=column,
    )
    scenario = _scenario(
        _query_step(statement, parameters={"value": 7}),
        outputs={DATABASE_QUERY_OUTPUT: "${query}"},
    )

    validate_database_draft(scenario, evidence)


def test_query_can_add_verification_of_complete_result_envelope() -> None:
    evidence = _table_evidence()
    verification = StepDefinition(
        activity="verifyDatabaseResult",
        params={
            "id": "verify",
            "actual": "${query}",
            "expected": {"result": [{"id": 7}]},
            "matchMode": "contains",
        },
    )
    scenario = _scenario(
        _query_step(
            'SELECT "id" FROM "public"."customers" WHERE "id" = :value',
            parameters={"value": 7},
        ),
        verification,
        outputs={
            DATABASE_QUERY_OUTPUT: "${query}",
            DATABASE_VERIFICATION_OUTPUT: "${verify}",
        },
    )

    validate_database_draft(scenario, evidence)

    verification.params["actual"] = "${query.result}"
    with pytest.raises(DatabaseDraftValidationError, match="complete query result envelope"):
        validate_database_draft(scenario, evidence)


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ('SELECT id FROM "public"."customers"', {}),
        ('SELECT "unknown" FROM "public"."customers"', {}),
        ('SELECT * FROM "public"."customers"', {}),
        ('SELECT "id" FROM "public"."other"', {}),
        ('SELECT "id" FROM "public"."customers" WHERE "id" = :value', {}),
        ('SELECT "id" FROM "public"."customers" WHERE "id" = \'private\'', {}),
    ],
)
def test_query_rejects_unverified_identifiers_wildcards_and_inline_strings(
    statement: str,
    parameters: Mapping[str, object],
) -> None:
    scenario = _scenario(
        _query_step(statement, parameters=parameters),
        outputs={DATABASE_QUERY_OUTPUT: "${query}"},
    )

    with pytest.raises(DatabaseDraftValidationError):
        validate_database_draft(scenario, _table_evidence())


def test_agent_context_excludes_local_run_identity_and_paths() -> None:
    evidence = _table_evidence()

    context = database_authoring_context(evidence)

    assert RUN_ID not in context
    assert evidence.source_path not in context
    assert evidence.scenario not in context
    assert "env:APP_DB_URL" in context
    assert '"tableMetadata"' in context
