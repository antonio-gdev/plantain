"""Agent-facing read-only database activities."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from plantain.activities.database.verification import verify_result
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runtime import RunContext
from plantain.models.database import (
    DatabaseDiscoveryResult,
    DiscoverDatabaseParams,
    QueryDatabaseParams,
    QueryDatabaseResult,
    VerifyDatabaseResult,
    VerifyDatabaseResultParams,
)
from plantain.observability import get_logger

_MAX_DATABASE_TRACE_BYTES = 8 * 1_024
_T = TypeVar("_T")
logger = get_logger("activities.database")


async def discover_database(
    context: RunContext,
    params: DiscoverDatabaseParams,
) -> DatabaseDiscoveryResult:
    """Execute exactly one scoped SQLAlchemy Inspector phase."""

    database = await context.services.database()

    async def execute() -> DatabaseDiscoveryResult:
        return await database.discover(params)

    return await _run_database_operation(
        context,
        phase="discovery",
        operation_type="inspect",
        target=_discovery_target(params),
        operation_input={
            "includeViews": params.include_views,
            "includeSystemSchemas": params.include_system_schemas,
            "pageSize": params.page_size,
            "cursorProvided": params.cursor is not None,
        },
        operation_expected=None,
        execute=execute,
        actual=_discovery_evidence,
    )


async def query_database(
    context: RunContext,
    params: QueryDatabaseParams,
) -> QueryDatabaseResult:
    """Execute one bounded, parameterized SELECT and retain it for chaining."""

    database = await context.services.database()

    async def execute() -> QueryDatabaseResult:
        return await database.query(params)

    return await _run_database_operation(
        context,
        phase="query",
        operation_type="SELECT",
        target=_query_target(params),
        operation_input={
            "parameters": params.parameters,
            "resultMode": params.effective_result_mode.value,
            "maxRows": params.max_rows,
        },
        operation_expected={"readOnly": True},
        execute=execute,
        actual=_query_evidence,
    )


async def verify_database_result(
    context: RunContext,
    params: VerifyDatabaseResultParams,
) -> VerifyDatabaseResult:
    """Assert a chained result without opening another database connection."""

    async def execute() -> VerifyDatabaseResult:
        return verify_result(params)

    return await _run_database_operation(
        context,
        phase="verification",
        operation_type="verify",
        target=f"matchMode={params.match_mode.value}",
        operation_input=params.actual,
        operation_expected=params.expected,
        execute=execute,
        actual=_verification_evidence,
    )


def _discovery_target(params: DiscoverDatabaseParams) -> str:
    parts = [f"phase={params.phase.value}"]
    if params.schema_name is not None:
        parts.append(f"schema={params.schema_name}")
    if params.table is not None:
        parts.append(f"table={params.table}")
    return " ".join(parts)


def _query_target(params: QueryDatabaseParams) -> str:
    if params.file is not None:
        return f"file={params.file}"
    digest = hashlib.sha256((params.sql or "").encode("utf-8")).hexdigest()[:16]
    return f"inline-select sha256={digest}"


def _discovery_evidence(result: DatabaseDiscoveryResult) -> dict[str, Any]:
    return result.model_dump(mode="json", by_alias=True)


def _query_evidence(result: QueryDatabaseResult) -> dict[str, Any]:
    return result.model_dump(mode="json", by_alias=True)


def _verification_evidence(result: VerifyDatabaseResult) -> dict[str, Any]:
    return result.model_dump(mode="json", by_alias=True)


async def _run_database_operation(
    context: RunContext,
    *,
    phase: str,
    operation_type: str,
    target: str,
    operation_input: object | None,
    operation_expected: object | None,
    execute: Callable[[], Awaitable[_T]],
    actual: Callable[[_T], object | None],
) -> _T:
    safe_input = _bounded_database_value(context, operation_input)
    safe_expected = _bounded_database_value(context, operation_expected)
    extra = {
        "scenario": context.scenario.scenario,
        "activity": context.current_activity or "database",
        "step_id": context.current_step_id or "database",
        "operation_type": operation_type,
        "operation_target": target,
        "operation_input": safe_input,
        "operation_expected": safe_expected,
    }
    logger.info("Database operation started", extra={**extra, "status": "running"})
    started = time.monotonic()
    try:
        result = await execute()
    except Exception as exc:
        duration_ms = max(0, round((time.monotonic() - started) * 1_000))
        context.add_operation(
            domain="database",
            phase=phase,
            operation_type=operation_type,
            target=target,
            status="failed",
            duration_ms=duration_ms,
            operation_input=safe_input,
            operation_expected=safe_expected,
            error_type=type(exc).__name__,
        )
        logger.error(  # noqa: TRY400 - tracebacks may expose database data.
            "Database operation failed",
            extra={
                **extra,
                "status": "failed",
                "duration_ms": duration_ms,
                "operation_error_type": type(exc).__name__,
            },
        )
        raise
    duration_ms = max(0, round((time.monotonic() - started) * 1_000))
    safe_actual = _bounded_database_value(context, actual(result))
    context.add_operation(
        domain="database",
        phase=phase,
        operation_type=operation_type,
        target=target,
        status="passed",
        duration_ms=duration_ms,
        operation_input=safe_input,
        operation_expected=safe_expected,
        operation_actual=safe_actual,
    )
    logger.info(
        "Database operation passed",
        extra={
            **extra,
            "status": "passed",
            "duration_ms": duration_ms,
            "operation_actual": safe_actual,
        },
    )
    return result


def _bounded_database_value(context: RunContext, value: Any) -> Any:
    if value is None:
        return None
    safe = context.secrets.redact(value)
    try:
        rendered = json.dumps(
            safe,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return "<unavailable>"
    encoded = rendered.encode("utf-8")
    if len(encoded) <= _MAX_DATABASE_TRACE_BYTES:
        return safe
    preview = encoded[:_MAX_DATABASE_TRACE_BYTES].decode("utf-8", errors="ignore")
    return {"preview": f"{preview}…", "truncated": True}


def register_database_activities(registry: ActivityRegistry) -> None:
    """Expose read-only activities; human mutation is deliberately not registered."""

    registry.register(
        "discoverDatabase",
        DiscoverDatabaseParams,
        discover_database,
        description=(
            "Discover schemas, tables/views, or one table's complete metadata through "
            "SQLAlchemy Inspector without reading application rows"
        ),
    )
    registry.register(
        "queryDatabase",
        QueryDatabaseParams,
        query_database,
        description=(
            "Execute one parameterized, proven read-only SELECT with bounded streaming results"
        ),
    )
    registry.register(
        "verifyDatabaseResult",
        VerifyDatabaseResultParams,
        verify_database_result,
        description="Verify a chained database result without another database call",
    )
