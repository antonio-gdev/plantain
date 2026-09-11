"""Complete bounded database-pool ownership coverage with fake engines."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import URL
from sqlalchemy.engine import Engine

from plantain.activities.database import pool
from plantain.activities.database.pool import (
    DatabasePoolError,
    DatabasePoolManager,
    _install_driver_timeout,
    _PoolEntry,
)
from plantain.activities.database.source import DatabaseVendor, ResolvedDatabaseSource
from plantain.models.database import DatabaseSource
from plantain.security import url_policy

DUMMY_CREDENTIAL = "synthetic-pool-credential"
EXPECTED_TIMEOUT_SECONDS = 2
EXPECTED_CONCURRENT_ENGINES = 2


class FakeEngine:
    """Disposable engine with optional safe failure injection."""

    def __init__(self, *, fail_dispose: bool = False) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self.dispose_calls = 0
        self.fail_dispose = fail_dispose

    def dispose(self, *, close: bool) -> None:
        assert close is True
        self.dispose_calls += 1
        if self.fail_dispose:
            raise RuntimeError("synthetic disposal detail")


def _settings(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "environment": "production",
        "egress_control_enforced": True,
        "allow_insecure_local_db_tls": False,
        "db_allowed_targets": ("db.example.test:5432",),
        "db_allow_private_networks": True,
        "db_pool_size": 5,
        "db_max_overflow": 0,
        "db_pool_timeout_seconds": 5.0,
        "db_pool_recycle_seconds": 1_800,
        "db_max_source_pools": 4,
        "db_max_sources_per_scenario": 4,
        "db_fetch_batch_size": 100,
        "db_query_timeout_seconds": 1.2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _resolve_database_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        url_policy,
        "_resolve_isolated_sync",
        lambda _admission, _host, _port, _cancelled: {"10.0.0.8"},
    )


def _source(database: str) -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "automation_reader",
            "password": DUMMY_CREDENTIAL,
            "dbUrl": f"jdbc:postgresql://db.example.test/{database}",
        }
    )


def _resolved(vendor: DatabaseVendor = DatabaseVendor.POSTGRESQL) -> ResolvedDatabaseSource:
    return ResolvedDatabaseSource(
        vendor=vendor,
        sqlalchemy_url=URL.create("postgresql+psycopg", host="db.example.test"),
        port=5432,
        target_identity=f"{vendor.value}\0db.example.test\0{5432}\0app",
        host="db.example.test",
        database="app",
    )


def _install_fake_creation(
    monkeypatch: pytest.MonkeyPatch,
    engines: list[FakeEngine],
) -> None:
    def create_engine(_url: Any, **_options: Any) -> FakeEngine:
        engine = FakeEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(pool, "create_engine", create_engine)
    monkeypatch.setattr(pool, "install_agent_read_only_firewall", lambda _engine: None)


def test_lease_release_is_idempotent_and_unknown_key_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engines: list[FakeEngine] = []
    _install_fake_creation(monkeypatch, engines)
    manager = DatabasePoolManager(cast("Any", _settings()))
    lease = manager.acquire(_source("app"))

    lease.release()
    lease.release()
    manager.release("unknown")

    assert manager._entries[lease.key].leases == 0
    manager.close()


def test_quarantine_blocks_reuse_and_replaces_engine_after_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engines: list[FakeEngine] = []
    _install_fake_creation(monkeypatch, engines)
    manager = DatabasePoolManager(cast("Any", _settings()))
    source = _source("app")
    lease = manager.acquire(source)

    lease.quarantine()
    lease.quarantine()

    assert manager._entries[lease.key].quarantined is True
    with pytest.raises(DatabasePoolError, match="quarantined"):
        manager.acquire(source)
    assert engines[0].dispose_calls == 0

    lease.release()
    assert lease.key not in manager._entries
    assert engines[0].dispose_calls == 1

    replacement = manager.acquire(source)
    assert replacement.engine is not lease.engine
    replacement.release()
    manager.close()
    assert engines[1].dispose_calls == 1


def test_close_defers_active_engine_disposal_until_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engines: list[FakeEngine] = []
    _install_fake_creation(monkeypatch, engines)
    manager = DatabasePoolManager(cast("Any", _settings()))
    lease = manager.acquire(_source("app"))

    manager.close()
    manager.close()

    assert manager.closed is True
    assert lease.key in manager._entries
    assert engines[0].dispose_calls == 0

    lease.release()
    assert lease.key not in manager._entries
    assert engines[0].dispose_calls == 1


def test_release_rejects_inconsistent_lease_accounting() -> None:
    manager = DatabasePoolManager(cast("Any", _settings()))
    manager._entries["source"] = _PoolEntry(
        engine=cast("Engine", FakeEngine()),
        resolved=_resolved(),
        leases=0,
    )

    with pytest.raises(DatabasePoolError, match="accounting is inconsistent"):
        manager.release("source")


def test_failed_creation_after_lru_eviction_disposes_evicted_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_engine = FakeEngine()
    calls = 0

    def create_engine(_url: Any, **_options: Any) -> FakeEngine:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_engine
        raise RuntimeError("synthetic driver detail")

    monkeypatch.setattr(pool, "create_engine", create_engine)
    monkeypatch.setattr(pool, "install_agent_read_only_firewall", lambda _engine: None)
    manager = DatabasePoolManager(cast("Any", _settings(db_max_source_pools=1)))
    first = manager.acquire(_source("first"))
    first.release()

    with pytest.raises(DatabasePoolError, match="Unable to initialize") as captured:
        manager.acquire(_source("second"))

    assert "synthetic driver detail" not in str(captured.value)
    assert first_engine.dispose_calls == 1


def test_entry_initialization_failure_disposes_created_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(pool, "create_engine", lambda _url, **_options: engine)
    monkeypatch.setattr(pool, "_install_driver_timeout", lambda *_args: None)

    def reject_firewall(_engine: Any) -> None:
        raise RuntimeError("synthetic firewall detail")

    monkeypatch.setattr(pool, "install_agent_read_only_firewall", reject_firewall)
    manager = DatabasePoolManager(cast("Any", _settings()))

    with pytest.raises(DatabasePoolError, match="postgresql database driver") as captured:
        manager._create_entry(_resolved())
    assert "synthetic firewall detail" not in str(captured.value)
    assert engine.dispose_calls == 1


def test_unrelated_source_engines_are_created_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_gate = Barrier(EXPECTED_CONCURRENT_ENGINES)
    engines: list[FakeEngine] = []

    def create_engine(_url: Any, **_options: Any) -> FakeEngine:
        creation_gate.wait(timeout=1)
        engine = FakeEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(pool, "create_engine", create_engine)
    monkeypatch.setattr(pool, "install_agent_read_only_firewall", lambda _engine: None)
    manager = DatabasePoolManager(cast("Any", _settings()))
    with ThreadPoolExecutor(max_workers=EXPECTED_CONCURRENT_ENGINES) as executor:
        futures = [
            executor.submit(manager.acquire, _source(database)) for database in ("first", "second")
        ]
        leases = [future.result(timeout=2) for future in futures]

    assert len(engines) == EXPECTED_CONCURRENT_ENGINES
    assert leases[0].engine is not leases[1].engine
    for lease in leases:
        lease.release()
    manager.close()


def test_same_source_callers_share_inflight_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_started = Event()
    release_creation = Event()
    waiter_started = Event()
    engines: list[FakeEngine] = []

    def create_engine(_url: Any, **_options: Any) -> FakeEngine:
        creation_started.set()
        assert release_creation.wait(timeout=1)
        engine = FakeEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(pool, "create_engine", create_engine)
    monkeypatch.setattr(pool, "install_agent_read_only_firewall", lambda _engine: None)
    manager = DatabasePoolManager(cast("Any", _settings()))
    original_wait = manager._wait_for_creation

    def observe_wait(creation: Any, cancelled: Any) -> None:
        waiter_started.set()
        original_wait(creation, cancelled)

    monkeypatch.setattr(manager, "_wait_for_creation", observe_wait)
    with ThreadPoolExecutor(max_workers=EXPECTED_CONCURRENT_ENGINES) as executor:
        first = executor.submit(manager.acquire, _source("shared"))
        assert creation_started.wait(timeout=1)
        second = executor.submit(manager.acquire, _source("shared"))
        assert waiter_started.wait(timeout=1)
        release_creation.set()
        leases = [first.result(timeout=2), second.result(timeout=2)]

    assert len(engines) == 1
    assert leases[0].engine is leases[1].engine
    for lease in leases:
        lease.release()
    manager.close()


def test_close_aggregates_safe_disposal_failures() -> None:
    manager = DatabasePoolManager(cast("Any", _settings()))
    engine = FakeEngine(fail_dispose=True)
    manager._entries["source"] = _PoolEntry(
        engine=cast("Engine", engine),
        resolved=_resolved(),
    )

    with pytest.raises(ExceptionGroup, match="failed disposal") as captured:
        manager.close()

    assert manager.closed is True
    assert engine.dispose_calls == 1
    assert "synthetic disposal detail" not in str(captured.value)


def test_sql_server_timeout_hook_rounds_up_and_handles_cursor_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: list[Any] = []

    def listens_for(_engine: Any, _event_name: str) -> Any:
        def register(handler: Any) -> Any:
            handlers.append(handler)
            return handler

        return register

    monkeypatch.setattr(pool.event, "listens_for", listens_for)
    engine = cast("Engine", FakeEngine())
    _install_driver_timeout(engine, DatabaseVendor.POSTGRESQL, cast("Any", _settings()))
    assert handlers == []

    _install_driver_timeout(engine, DatabaseVendor.SQL_SERVER, cast("Any", _settings()))
    cursor = SimpleNamespace(timeout=0)
    handlers[0](None, cursor, "SELECT 1", None, None, False)
    handlers[0](None, object(), "SELECT 1", None, None, False)
    assert cursor.timeout == EXPECTED_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "setting, value, label",
    [
        ("db_pool_size", 0, "PLANTAIN_DB_POOL_SIZE"),
        ("db_max_overflow", pool.MAX_POOL_OVERFLOW + 1, "PLANTAIN_DB_MAX_OVERFLOW"),
        ("db_max_source_pools", 0, "PLANTAIN_DB_MAX_SOURCE_POOLS"),
        (
            "db_max_sources_per_scenario",
            pool.MAX_SOURCES_PER_SCENARIO + 1,
            "PLANTAIN_DB_MAX_SOURCES_PER_SCENARIO",
        ),
        ("db_fetch_batch_size", 0, "PLANTAIN_DB_FETCH_BATCH_SIZE"),
    ],
)
def test_every_pool_setting_is_bounded(setting: str, value: int, label: str) -> None:
    with pytest.raises(DatabasePoolError, match=label):
        DatabasePoolManager(cast("Any", _settings(**{setting: value})))


def test_combined_pool_capacity_is_bounded() -> None:
    settings = _settings(
        db_pool_size=pool.MAX_POOL_SIZE,
        db_max_overflow=pool.MAX_POOL_OVERFLOW,
        db_max_source_pools=pool.MAX_SOURCE_POOLS,
    )

    with pytest.raises(DatabasePoolError, match="Combined database pool capacity"):
        DatabasePoolManager(cast("Any", settings))
