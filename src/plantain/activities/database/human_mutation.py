"""Explicit human-only DML path, intentionally absent from the activity registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from plantain.activities.database.source import (
    DatabaseSourceError,
    DatabaseTlsPolicy,
    database_source_from_environment,
    resolve_database_source,
)
from plantain.config import Settings
from plantain.engine.admission import ResourceAdmission
from plantain.errors import ConfigurationError
from plantain.security.url_policy import TcpTargetPolicy

HUMAN_MUTATION_ACKNOWLEDGEMENT = "I AUTHORIZE THIS DATABASE MUTATION"
MAX_HUMAN_SQL_FILE_BYTES = 1_000_000
_ALLOWED_DML = (exp.Delete, exp.Insert, exp.Merge, exp.Update)


class HumanMutationError(RuntimeError):
    """Raised when explicit human mutation authorization or execution fails."""


@dataclass(frozen=True, slots=True)
class HumanMutationResult:
    """Non-sensitive mutation receipt."""

    statement_type: str
    affected_rows: int | None


async def execute_human_mutation_file(
    settings: Settings,
    *,
    sql_file: Path,
    parameters: dict[str, Any],
    acknowledgement: str,
) -> HumanMutationResult:
    """Run one reviewed DML file after independent environment and CLI consent."""

    if not settings.allow_db_mutations:
        raise HumanMutationError("PLANTAIN_ALLOW_DB_MUTATIONS must be true")
    if acknowledgement != HUMAN_MUTATION_ACKNOWLEDGEMENT:
        raise HumanMutationError("The exact human mutation acknowledgement is required")
    admission = ResourceAdmission(settings)
    statement = await admission.run_blocking(_read_human_sql, settings, sql_file)
    cancelled = Event()
    try:
        return await admission.run_database_operation(
            _execute_sync,
            settings,
            statement,
            parameters,
            admission=admission,
            cancelled=cancelled,
        )
    except asyncio.CancelledError:
        cancelled.set()
        raise


def _read_human_sql(settings: Settings, sql_file: Path) -> str:
    """Read one contained SQL file without blocking the async runner."""

    root = settings.project_root.resolve()
    candidate = sql_file.expanduser()
    target = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HumanMutationError("Human SQL files must stay inside the project root") from exc
    if not target.is_file():
        raise HumanMutationError("Human SQL file is missing")
    try:
        with target.open("rb") as handle:
            payload = handle.read(MAX_HUMAN_SQL_FILE_BYTES + 1)
    except OSError as exc:
        raise HumanMutationError("Human SQL file must be readable UTF-8") from exc
    if len(payload) > MAX_HUMAN_SQL_FILE_BYTES:
        raise HumanMutationError("Human SQL file exceeds 1 MB")
    try:
        statement = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise HumanMutationError("Human SQL file must be readable UTF-8") from exc
    if not statement:
        raise HumanMutationError("Human SQL file must contain one DML statement")
    return statement


def _execute_sync(
    settings: Settings,
    statement: str,
    parameters: dict[str, Any],
    *,
    admission: ResourceAdmission | None = None,
    cancelled: Event | None = None,
) -> HumanMutationResult:
    if ";" in statement.rstrip(";"):
        raise HumanMutationError("Exactly one DML statement is permitted")
    try:
        expression = parse_one(statement)
    except ParseError as exc:
        raise HumanMutationError("Human SQL file could not be parsed as one statement") from exc
    if not isinstance(expression, _ALLOWED_DML):
        raise HumanMutationError("Human mutation permits INSERT, UPDATE, DELETE, or MERGE only")
    try:
        source = database_source_from_environment()
        resolved = resolve_database_source(
            source,
            tls_policy=DatabaseTlsPolicy(
                environment=settings.environment,
                allow_insecure_local_tls=settings.allow_insecure_local_db_tls,
            ),
            read_only_intent=False,
        )
    except DatabaseSourceError as exc:
        raise HumanMutationError(str(exc)) from exc
    target_policy = TcpTargetPolicy(
        allowed_targets=getattr(settings, "db_allowed_targets", ()),
        allow_private_networks=getattr(settings, "db_allow_private_networks", False),
        environment=getattr(settings, "environment", "production"),
        egress_control_enforced=getattr(settings, "egress_control_enforced", False),
    )
    try:
        target_policy.validate(
            resolved.host,
            resolved.port,
            admission=admission or ResourceAdmission(settings),
            cancelled=cancelled or Event(),
        )
    except ConfigurationError as exc:
        raise HumanMutationError(
            "Human database target was blocked by outbound network policy"
        ) from exc
    engine = None
    try:
        engine = create_engine(
            resolved.sqlalchemy_url,
            connect_args=dict(resolved.connect_args),
            pool_pre_ping=True,
            poolclass=NullPool,
        )
        with engine.begin() as connection:
            result = connection.execute(text(statement), parameters)
            row_count = result.rowcount if result.rowcount >= 0 else None
        return HumanMutationResult(
            statement_type=type(expression).__name__.upper(),
            affected_rows=row_count,
        )
    except SQLAlchemyError as exc:
        raise HumanMutationError(
            f"Human-authorized mutation failed ({type(exc).__name__})"
        ) from exc
    finally:
        if engine is not None:
            engine.dispose(close=True)
