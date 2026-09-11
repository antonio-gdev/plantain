"""Bounded shared SQLAlchemy engine-pool lifecycle tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

import plantain.activities.database.pool as pool_module
from plantain.activities.database.pool import DatabasePoolError, DatabasePoolManager
from plantain.models.database import DatabaseSource
from plantain.security import url_policy

DEFAULT_CREDENTIAL_VALUE = "default-unit-value"
FIRST_CREDENTIAL_VALUE = "first-unit-value"
ROTATED_CREDENTIAL_VALUE = "rotated-unit-value"
EXPECTED_POOL_SIZE = 5
EXPECTED_ENGINE_COUNT = 2


class FakeEngine:
    def __init__(self) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self.dispose_calls = 0

    def dispose(self, *, close: bool) -> None:
        assert close is True
        self.dispose_calls += 1


def _settings(**overrides: Any) -> SimpleNamespace:
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
        "db_query_timeout_seconds": 15.0,
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


def _source(
    database: str,
    *,
    credential: str = DEFAULT_CREDENTIAL_VALUE,
) -> DatabaseSource:
    return DatabaseSource.model_validate(
        {
            "username": "reader",
            "password": credential,
            "dbUrl": f"jdbc:postgresql://db.example.test/{database}",
        }
    )


@pytest.fixture
def engine_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[FakeEngine], list[dict[str, Any]]]:
    engines: list[FakeEngine] = []
    options: list[dict[str, Any]] = []

    def create_engine(_url: object, **kwargs: Any) -> FakeEngine:
        engine = FakeEngine()
        engines.append(engine)
        options.append(kwargs)
        return engine

    monkeypatch.setattr(pool_module, "create_engine", create_engine)
    monkeypatch.setattr(
        pool_module,
        "install_agent_read_only_firewall",
        lambda _engine: None,
    )
    return engines, options


def test_reuses_one_engine_for_identical_credentials_and_pool_settings(
    engine_factory: tuple[list[FakeEngine], list[dict[str, Any]]],
) -> None:
    engines, options = engine_factory
    manager = DatabasePoolManager(cast("Any", _settings()))
    first = manager.acquire(_source("clothing"))
    second = manager.acquire(_source("clothing"))

    assert first.engine is second.engine
    assert len(engines) == 1
    assert options[0]["pool_size"] == EXPECTED_POOL_SIZE
    assert options[0]["max_overflow"] == 0
    assert options[0]["pool_pre_ping"] is True
    assert options[0]["pool_reset_on_return"] == "rollback"
    assert options[0]["pool_use_lifo"] is True
    first.release()
    second.release()
    manager.close()
    assert engines[0].dispose_calls == 1


def test_credential_rotation_creates_a_distinct_non_revealing_pool_key(
    engine_factory: tuple[list[FakeEngine], list[dict[str, Any]]],
) -> None:
    engines, _options = engine_factory
    manager = DatabasePoolManager(cast("Any", _settings()))
    first = manager.acquire(_source("clothing", credential=FIRST_CREDENTIAL_VALUE))
    second = manager.acquire(_source("clothing", credential=ROTATED_CREDENTIAL_VALUE))

    assert first.key != second.key
    assert FIRST_CREDENTIAL_VALUE not in first.key
    assert ROTATED_CREDENTIAL_VALUE not in second.key
    assert len(engines) == EXPECTED_ENGINE_COUNT
    first.release()
    second.release()
    manager.close()


def test_lru_evicts_only_idle_sources_and_active_cap_fails_closed(
    engine_factory: tuple[list[FakeEngine], list[dict[str, Any]]],
) -> None:
    engines, _options = engine_factory
    manager = DatabasePoolManager(cast("Any", _settings(db_max_source_pools=1)))
    first = manager.acquire(_source("first"))
    with pytest.raises(DatabasePoolError, match="Active database sources"):
        manager.acquire(_source("second"))
    first.release()

    second = manager.acquire(_source("second"))

    assert len(engines) == EXPECTED_ENGINE_COUNT
    assert engines[0].dispose_calls == 1
    second.release()
    manager.close()
    assert engines[1].dispose_calls == 1


def test_close_is_idempotent_and_closed_manager_rejects_new_leases(
    engine_factory: tuple[list[FakeEngine], list[dict[str, Any]]],
) -> None:
    engines, _options = engine_factory
    manager = DatabasePoolManager(cast("Any", _settings()))
    lease = manager.acquire(_source("clothing"))
    lease.release()
    manager.close()
    manager.close()

    assert manager.closed is True
    assert engines[0].dispose_calls == 1
    with pytest.raises(DatabasePoolError, match="closed"):
        manager.acquire(_source("other"))


def test_invalid_pool_bounds_fail_before_any_engine_is_created(
    engine_factory: tuple[list[FakeEngine], list[dict[str, Any]]],
) -> None:
    engines, _options = engine_factory

    with pytest.raises(DatabasePoolError, match="PLANTAIN_DB_POOL_SIZE"):
        DatabasePoolManager(cast("Any", _settings(db_pool_size=33)))
    assert engines == []


def test_unallowlisted_target_fails_before_engine_creation(
    engine_factory: tuple[list[FakeEngine], list[dict[str, Any]]],
) -> None:
    engines, _options = engine_factory
    manager = DatabasePoolManager(cast("Any", _settings(db_allowed_targets=())))

    with pytest.raises(DatabasePoolError, match="outbound network policy"):
        manager.acquire(_source("clothing"))

    assert engines == []
    assert manager.closed is False


def test_non_local_target_requires_external_egress_control(
    engine_factory: tuple[list[FakeEngine], list[dict[str, Any]]],
) -> None:
    engines, _options = engine_factory
    manager = DatabasePoolManager(cast("Any", _settings(egress_control_enforced=False)))

    with pytest.raises(DatabasePoolError, match="outbound network policy"):
        manager.acquire(_source("clothing"))

    assert engines == []
    assert manager.closed is False
    manager.close()
    assert manager.closed is True
