"""Scenario-local database service over shared SQLAlchemy Engine pools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Event, Lock
from typing import TypeVar

from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from plantain.activities.database.discovery import discover_metadata
from plantain.activities.database.errors import DatabaseActivityError
from plantain.activities.database.policy import DatabasePolicyError
from plantain.activities.database.pool import (
    DatabaseLease,
    DatabasePoolError,
    DatabasePoolManager,
)
from plantain.activities.database.query import execute_query
from plantain.activities.database.source import DatabaseSourceError
from plantain.config import Settings
from plantain.engine.admission import ResourceAdmission
from plantain.models.database import (
    DatabaseDiscoveryResult,
    DatabaseSource,
    DiscoverDatabaseParams,
    QueryDatabaseParams,
    QueryDatabaseResult,
)

MILLISECONDS_PER_SECOND = 1_000
_T = TypeVar("_T")


class _DatabaseOperationState:
    """Share abandonment state safely between the event loop and one worker."""

    def __init__(self) -> None:
        self._abandoned = Event()

    @property
    def abandoned(self) -> bool:
        return self._abandoned.is_set()

    def abandon(self) -> None:
        self._abandoned.set()

    @property
    def cancellation(self) -> Event:
        return self._abandoned


class DatabaseSession:
    """Isolate scenario source usage while leasing shared SQLAlchemy Engines."""

    def __init__(
        self,
        settings: Settings,
        pools: DatabasePoolManager,
        *,
        admission: ResourceAdmission | None = None,
    ) -> None:
        self._settings = settings
        self._pools = pools
        self._admission = admission or ResourceAdmission(settings)
        self._source_keys: set[str] = set()
        self._state_lock = Lock()
        self._closed = False

    async def discover(self, params: DiscoverDatabaseParams) -> DatabaseDiscoveryResult:
        """Run one metadata-only discovery phase in a worker thread."""

        return await self._bounded_operation(
            "Database discovery",
            lambda state: self._with_connection(
                params.source,
                lambda connection: discover_metadata(
                    connection,
                    params,
                    max_result_bytes=self._settings.db_max_result_bytes,
                ),
                state=state,
            ),
        )

    async def query(self, params: QueryDatabaseParams) -> QueryDatabaseResult:
        """Run one bounded, parameterized SELECT in a worker thread."""

        return await self._bounded_operation(
            "Database query",
            lambda state: self._with_connection(
                params.source,
                lambda connection: execute_query(connection, params, self._settings),
                state=state,
            ),
        )

    async def close(self) -> None:
        """Prevent new work; individual operation leases release in their finally blocks."""

        with self._state_lock:
            self._closed = True

    async def _bounded_operation(
        self,
        label: str,
        operation: Callable[[_DatabaseOperationState], _T],
    ) -> _T:
        state = _DatabaseOperationState()
        try:
            async with asyncio.timeout(self._settings.db_query_timeout_seconds):
                return await self._admission.run_database_operation(operation, state)
        except TimeoutError as exc:
            state.abandon()
            raise DatabaseActivityError(
                f"{label} exceeded PLANTAIN_DB_QUERY_TIMEOUT_SECONDS"
            ) from exc
        except asyncio.CancelledError:
            state.abandon()
            raise
        except DatabaseActivityError:
            raise
        except (DatabasePolicyError, DatabasePoolError, DatabaseSourceError) as exc:
            raise DatabaseActivityError(str(exc)) from exc
        except SQLAlchemyError as exc:
            raise DatabaseActivityError(f"{label} failed ({type(exc).__name__})") from exc

    def _with_connection(
        self,
        source: DatabaseSource,
        operation: Callable[[Connection], _T],
        *,
        state: _DatabaseOperationState | None = None,
    ) -> _T:
        self._assert_open()
        lease = self._pools.acquire(
            source,
            admission=self._admission,
            cancelled=state.cancellation if state is not None else None,
        )
        try:
            self._observe_source(lease)
            with lease.engine.connect() as connection:
                self._configure_read_only(connection)
                return operation(connection)
        finally:
            if state is not None and state.abandoned:
                lease.quarantine()
            lease.release()

    def _assert_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise DatabaseActivityError("Database scenario session is closed")

    def _observe_source(self, lease: DatabaseLease) -> None:
        with self._state_lock:
            self._source_keys.add(lease.key)
            if len(self._source_keys) > self._settings.db_max_sources_per_scenario:
                self._source_keys.remove(lease.key)
                raise DatabaseActivityError(
                    "Scenario database sources exceed PLANTAIN_DB_MAX_SOURCES_PER_SCENARIO"
                )

    def _configure_read_only(self, connection: Connection) -> None:
        dialect = connection.dialect.name
        setup_options = {"plantain_internal_readonly_setup": True}
        milliseconds = max(
            1,
            round(self._settings.db_query_timeout_seconds * MILLISECONDS_PER_SECOND),
        )
        try:
            if dialect == "postgresql":
                connection.exec_driver_sql(
                    "SET TRANSACTION READ ONLY",
                    execution_options=setup_options,
                )
                connection.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {milliseconds}",
                    execution_options=setup_options,
                )
            elif dialect == "mysql":
                connection.exec_driver_sql(
                    "SET TRANSACTION READ ONLY",
                    execution_options=setup_options,
                )
                connection.exec_driver_sql(
                    f"SET SESSION MAX_EXECUTION_TIME = {milliseconds}",
                    execution_options=setup_options,
                )
            elif dialect == "oracle":
                connection.exec_driver_sql(
                    "SET TRANSACTION READ ONLY",
                    execution_options=setup_options,
                )
                driver_connection = connection.connection.driver_connection
                if driver_connection is None or not hasattr(driver_connection, "call_timeout"):
                    raise DatabaseActivityError(
                        "Oracle driver does not expose a query timeout control"
                    )
                driver_connection.call_timeout = milliseconds
            elif dialect != "mssql":
                raise DatabaseActivityError(
                    "Database driver resolved to an unsupported SQLAlchemy dialect"
                )
        except SQLAlchemyError as exc:
            raise DatabaseActivityError(
                f"Unable to enforce database read-only mode for dialect '{dialect}'"
            ) from exc


__all__ = ["DatabaseSession"]
