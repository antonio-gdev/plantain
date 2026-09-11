"""Scenario-local database enforcement and lifecycle tests."""

from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.database.errors import DatabaseActivityError
from plantain.activities.database.pool import DatabasePoolError
from plantain.activities.database.service import DatabaseSession
from plantain.models.database import DatabaseSource

EXPECTED_TIMEOUT_MILLISECONDS = 1_250


class FakeConnection:
    def __init__(self, dialect: str, *, driver_connection: object | None = None) -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.connection = SimpleNamespace(driver_connection=driver_connection)
        self.statements: list[tuple[str, dict[str, bool]]] = []

    def exec_driver_sql(
        self,
        statement: str,
        *,
        execution_options: dict[str, bool],
    ) -> None:
        self.statements.append((statement, execution_options))

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def connect(self) -> FakeConnection:
        return self._connection


class FakeLease:
    def __init__(self, key: str, connection: FakeConnection) -> None:
        self.key = key
        self.engine = FakeEngine(connection)
        self.quarantine_calls = 0
        self.release_calls = 0
        self.released = Event()

    def quarantine(self) -> None:
        self.quarantine_calls += 1

    def release(self) -> None:
        self.release_calls += 1
        self.released.set()


class FakePools:
    def __init__(self, leases: list[FakeLease]) -> None:
        self._leases = iter(leases)
        self.acquire_calls = 0
        self.admissions: list[object | None] = []
        self.cancellations: list[Event | None] = []
        self.acquired = Event()

    def acquire(
        self,
        _source: DatabaseSource,
        *,
        admission: object | None = None,
        cancelled: Event | None = None,
    ) -> FakeLease:
        self.acquire_calls += 1
        self.admissions.append(admission)
        self.cancellations.append(cancelled)
        self.acquired.set()
        return next(self._leases)


def _settings(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "db_query_timeout_seconds": 1.25,
        "db_max_sources_per_scenario": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _source() -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "readonly-user",
            "password": "unit-test-credential",
            "dbUrl": "jdbc:postgresql://db.example.test/clothing",
        }
    )


@pytest.mark.parametrize(
    ("dialect", "expected_statements"),
    [
        (
            "postgresql",
            ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = 1250"],
        ),
        (
            "mysql",
            ["SET TRANSACTION READ ONLY", "SET SESSION MAX_EXECUTION_TIME = 1250"],
        ),
        ("mssql", []),
    ],
)
def test_configures_supported_read_only_sessions(
    dialect: str,
    expected_statements: list[str],
) -> None:
    session = DatabaseSession(cast("Any", _settings()), cast("Any", FakePools([])))
    connection = FakeConnection(dialect)

    session._configure_read_only(cast("Any", connection))

    assert [statement for statement, _options in connection.statements] == expected_statements
    assert all(
        options == {"plantain_internal_readonly_setup": True}
        for _statement, options in connection.statements
    )


def test_oracle_sets_native_driver_call_timeout() -> None:
    driver_connection = SimpleNamespace(call_timeout=0)
    connection = FakeConnection("oracle", driver_connection=driver_connection)
    session = DatabaseSession(cast("Any", _settings()), cast("Any", FakePools([])))

    session._configure_read_only(cast("Any", connection))

    assert [statement for statement, _options in connection.statements] == [
        "SET TRANSACTION READ ONLY"
    ]
    assert driver_connection.call_timeout == EXPECTED_TIMEOUT_MILLISECONDS


def test_oracle_fails_closed_without_native_timeout_control() -> None:
    connection = FakeConnection("oracle", driver_connection=object())
    session = DatabaseSession(cast("Any", _settings()), cast("Any", FakePools([])))

    with pytest.raises(DatabaseActivityError, match="does not expose"):
        session._configure_read_only(cast("Any", connection))


def test_unsupported_dialect_fails_before_activity_execution() -> None:
    connection = FakeConnection("sqlite")
    session = DatabaseSession(cast("Any", _settings()), cast("Any", FakePools([])))

    with pytest.raises(DatabaseActivityError, match="unsupported SQLAlchemy dialect"):
        session._configure_read_only(cast("Any", connection))


def test_source_cap_releases_the_rejected_lease() -> None:
    first = FakeLease("first-source", FakeConnection("mssql"))
    second = FakeLease("second-source", FakeConnection("mssql"))
    pools = FakePools([first, second])
    session = DatabaseSession(
        cast("Any", _settings(db_max_sources_per_scenario=1)),
        cast("Any", pools),
    )

    assert session._with_connection(_source(), lambda _connection: "ok") == "ok"
    with pytest.raises(DatabaseActivityError, match="DB_MAX_SOURCES_PER_SCENARIO"):
        session._with_connection(_source(), lambda _connection: "unreachable")

    assert first.release_calls == 1
    assert second.release_calls == 1


def test_operation_failure_always_releases_lease() -> None:
    lease = FakeLease("source", FakeConnection("mssql"))
    session = DatabaseSession(
        cast("Any", _settings()),
        cast("Any", FakePools([lease])),
    )

    def fail(_connection: object) -> None:
        raise RuntimeError("controlled unit failure")

    with pytest.raises(RuntimeError, match="controlled unit failure"):
        session._with_connection(_source(), fail)

    assert lease.release_calls == 1


def test_closed_session_rejects_work_before_acquiring_a_lease() -> None:
    pools = FakePools([])
    session = DatabaseSession(cast("Any", _settings()), cast("Any", pools))
    asyncio.run(session.close())

    with pytest.raises(DatabaseActivityError, match="session is closed"):
        session._with_connection(_source(), lambda _connection: None)

    assert pools.acquire_calls == 0


def test_pool_errors_are_translated_to_activity_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def immediate_to_thread(operation: object, *args: object) -> object:
        return cast("Any", operation)(*args)

    def fail(_state: object) -> None:
        raise DatabasePoolError("Database pool manager is closed")

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    session = DatabaseSession(cast("Any", _settings()), cast("Any", FakePools([])))

    with pytest.raises(DatabaseActivityError, match="pool manager is closed"):
        asyncio.run(session._bounded_operation("Database query", fail))


def test_async_timeout_uses_safe_configuration_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def delayed_to_thread(_operation: object, *_args: object) -> None:
        await asyncio.sleep(0.02)

    monkeypatch.setattr(asyncio, "to_thread", delayed_to_thread)
    session = DatabaseSession(
        cast("Any", _settings(db_query_timeout_seconds=0.001)),
        cast("Any", FakePools([])),
    )

    with pytest.raises(
        DatabaseActivityError,
        match="exceeded PLANTAIN_DB_QUERY_TIMEOUT_SECONDS",
    ):
        asyncio.run(session._bounded_operation("Database query", lambda _state: None))


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancellation"])
def test_abandoned_operation_quarantines_only_after_worker_cleanup(
    *,
    cancel: bool,
) -> None:
    lease = FakeLease("source", FakeConnection("mssql"))
    release = Event()
    timeout_seconds = 1.0 if cancel else 0.01
    pools = FakePools([lease])
    session = DatabaseSession(
        cast("Any", _settings(db_query_timeout_seconds=timeout_seconds)),
        cast("Any", pools),
    )

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        finished = asyncio.Event()

        def operation(state: object) -> bool:
            loop.call_soon_threadsafe(started.set)
            try:
                return session._with_connection(
                    _source(),
                    lambda _connection: release.wait(timeout=1),
                    state=cast("Any", state),
                )
            finally:
                loop.call_soon_threadsafe(finished.set)

        # Keep a task handle so cancellation occurs only after the worker starts.
        task = asyncio.create_task(session._bounded_operation("Database query", operation))
        await asyncio.wait_for(started.wait(), timeout=1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(
                DatabaseActivityError,
                match="exceeded PLANTAIN_DB_QUERY_TIMEOUT_SECONDS",
            ):
                await task

        assert await asyncio.to_thread(pools.acquired.wait, 1)
        assert lease.quarantine_calls == 0
        assert lease.release_calls == 0
        assert len(pools.cancellations) == 1
        assert pools.admissions[0] is not None
        cancellation = pools.cancellations[0]
        assert cancellation is not None
        assert cancellation.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)

    asyncio.run(exercise())

    assert lease.quarantine_calls == 1
    assert lease.release_calls == 1
