"""Bounded lifecycle management for SQLAlchemy Engine connection pools."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from dataclasses import dataclass, field
from threading import Event, RLock
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from plantain.activities.database.policy import install_agent_read_only_firewall
from plantain.activities.database.source import (
    DatabaseTlsPolicy,
    DatabaseVendor,
    ResolvedDatabaseSource,
    resolve_database_source,
)
from plantain.config import (
    MAX_DATABASE_POOL_OVERFLOW,
    MAX_DATABASE_POOL_SIZE,
    MAX_DATABASE_SOURCE_POOLS,
    MAX_DATABASE_SOURCES_PER_SCENARIO,
    Settings,
)
from plantain.errors import ConfigurationError
from plantain.models.database import DatabaseSource
from plantain.security.url_policy import TcpTargetPolicy

if TYPE_CHECKING:
    from plantain.engine.admission import ResourceAdmission

MAX_POOL_SIZE = MAX_DATABASE_POOL_SIZE
MAX_POOL_OVERFLOW = MAX_DATABASE_POOL_OVERFLOW
MAX_SOURCE_POOLS = MAX_DATABASE_SOURCE_POOLS
MAX_TOTAL_POOL_CONNECTIONS = 256
MAX_SOURCES_PER_SCENARIO = MAX_DATABASE_SOURCES_PER_SCENARIO
MAX_FETCH_BATCH_SIZE = 1_000
_CREATION_WAIT_SECONDS = 0.05


class DatabasePoolError(RuntimeError):
    """Raised when a source pool cannot be created or leased safely."""


@dataclass(slots=True)
class _PoolEntry:
    engine: Engine
    resolved: ResolvedDatabaseSource
    leases: int = 0
    quarantined: bool = False
    last_used: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class _PoolCreation:
    done: Event = field(default_factory=Event)
    failure: DatabasePoolError | None = None


@dataclass(slots=True)
class DatabaseLease:
    """Idempotently releasable access to one SQLAlchemy Engine."""

    key: str
    engine: Engine = field(repr=False)
    resolved: ResolvedDatabaseSource = field(repr=False)
    _manager: DatabasePoolManager = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)
    _quarantined: bool = field(default=False, init=False, repr=False)

    def release(self) -> None:
        if not self._released:
            self._manager.release(self.key)
            self._released = True

    def quarantine(self) -> None:
        if not self._quarantined:
            self._quarantined = True
            self._manager.quarantine(self.key)


class DatabasePoolManager:
    """Own a bounded set of SQLAlchemy Engines for one runner execution."""

    def __init__(self, settings: Settings) -> None:
        _validate_pool_settings(settings)
        self._settings = settings
        self._key = secrets.token_bytes(32)
        self._entries: dict[str, _PoolEntry] = {}
        self._creations: dict[str, _PoolCreation] = {}
        self._lock = RLock()
        self._closed = False
        self._target_policy = TcpTargetPolicy(
            allowed_targets=getattr(settings, "db_allowed_targets", ()),
            allow_private_networks=getattr(
                settings,
                "db_allow_private_networks",
                False,
            ),
            environment=getattr(settings, "environment", "production"),
            egress_control_enforced=getattr(
                settings,
                "egress_control_enforced",
                False,
            ),
        )

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def acquire(
        self,
        source: DatabaseSource,
        *,
        admission: ResourceAdmission | None = None,
        cancelled: Event | None = None,
    ) -> DatabaseLease:
        """Acquire a scenario lease, reusing an existing source Engine when safe."""

        with self._lock:
            self._require_open_locked()
        resolved = resolve_database_source(
            source,
            tls_policy=DatabaseTlsPolicy(
                environment=self._settings.environment,
                allow_insecure_local_tls=self._settings.allow_insecure_local_db_tls,
            ),
        )
        try:
            self._target_policy.validate(
                resolved.host,
                resolved.port,
                admission=admission,
                cancelled=cancelled,
            )
        except ConfigurationError as exc:
            raise DatabasePoolError(
                "Database target was blocked by outbound network policy"
            ) from exc
        with self._lock:
            self._require_open_locked()
            key = self._source_key(source, resolved)
        while True:
            with self._lock:
                self._require_open_locked()
                entry = self._entries.get(key)
                self._require_reusable_entry(entry)
                if entry is not None:
                    return self._lease_locked(key, entry)
                creation = self._creations.get(key)
                if creation is None:
                    retired = self._evict_idle_locked()
                    creation = _PoolCreation()
                    self._creations[key] = creation
                    break
            self._wait_for_creation(creation, cancelled)
        return self._create_reserved(key, resolved, creation, retired)

    def release(self, key: str) -> None:
        """Release one scenario lease without disposing reusable pooled connections."""

        dispose_after_unlock: Engine | None = None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if entry.leases < 1:
                raise DatabasePoolError("Database source lease accounting is inconsistent")
            entry.leases -= 1
            entry.last_used = time.monotonic()
            if entry.leases == 0 and (entry.quarantined or self._closed):
                del self._entries[key]
                dispose_after_unlock = entry.engine
        if dispose_after_unlock is not None:
            self._dispose_retired(dispose_after_unlock)

    def quarantine(self, key: str) -> None:
        """Prevent reuse and retire the engine after its final active lease."""

        dispose_after_unlock: Engine | None = None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.quarantined = True
            if entry.leases == 0:
                del self._entries[key]
                dispose_after_unlock = entry.engine
        if dispose_after_unlock is not None:
            self._dispose_retired(dispose_after_unlock)

    def close(self) -> None:
        """Close the manager, disposing idle engines and retiring active ones later."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            entries: list[_PoolEntry] = []
            for key, entry in tuple(self._entries.items()):
                if entry.leases == 0:
                    del self._entries[key]
                    entries.append(entry)
            creations = tuple(self._creations.values())
            self._key = b""
        for creation in creations:
            creation.done.wait()
        errors: list[Exception] = []
        for entry in entries:
            try:
                entry.engine.dispose(close=True)
            except Exception as exc:  # noqa: BLE001 - dispose every remaining engine.
                errors.append(
                    DatabasePoolError(
                        f"A database engine failed safe disposal ({type(exc).__name__})"
                    )
                )
        if errors:
            raise ExceptionGroup("One or more database engines failed disposal", errors)

    @staticmethod
    def _dispose_retired(engine: Engine) -> None:
        try:
            engine.dispose(close=True)
        except Exception as exc:
            raise DatabasePoolError(
                f"A retired database engine failed safe disposal ({type(exc).__name__})"
            ) from exc

    def _source_key(
        self,
        source: DatabaseSource,
        resolved: ResolvedDatabaseSource,
    ) -> str:
        digest = hmac.new(self._key, digestmod=hashlib.sha256)
        for value in (
            resolved.target_identity,
            source.db_url,
            source.username,
            source.password.get_secret_value(),
        ):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _require_open_locked(self) -> None:
        if self._closed:
            raise DatabasePoolError("Database pool manager is closed")

    def _lease_locked(self, key: str, entry: _PoolEntry) -> DatabaseLease:
        entry.leases += 1
        entry.last_used = time.monotonic()
        return DatabaseLease(
            key=key,
            engine=entry.engine,
            resolved=entry.resolved,
            _manager=self,
        )

    @staticmethod
    def _wait_for_creation(
        creation: _PoolCreation,
        cancelled: Event | None,
    ) -> None:
        while not creation.done.wait(_CREATION_WAIT_SECONDS):
            if cancelled is not None and cancelled.is_set():
                raise DatabasePoolError("Database pool acquisition was cancelled safely")
        if cancelled is not None and cancelled.is_set():
            raise DatabasePoolError("Database pool acquisition was cancelled safely")
        if creation.failure is not None:
            raise creation.failure

    def _create_reserved(
        self,
        key: str,
        resolved: ResolvedDatabaseSource,
        creation: _PoolCreation,
        retired: Engine | None,
    ) -> DatabaseLease:
        try:
            if retired is not None:
                self._dispose_retired(retired)
            entry = self._create_entry(resolved)
        except BaseException as exc:
            failure = (
                exc
                if isinstance(exc, DatabasePoolError)
                else DatabasePoolError(
                    f"Database pool creation ended unexpectedly ({type(exc).__name__})"
                )
            )
            self._fail_creation(key, creation, failure)
            raise
        with self._lock:
            if not self._closed:
                self._entries[key] = entry
                lease = self._lease_locked(key, entry)
                del self._creations[key]
                creation.done.set()
                return lease
        try:
            self._dispose_retired(entry.engine)
        except DatabasePoolError as exc:
            failure = exc
        else:
            failure = DatabasePoolError("Database pool manager is closed")
        self._fail_creation(key, creation, failure)
        raise failure

    def _fail_creation(
        self,
        key: str,
        creation: _PoolCreation,
        failure: DatabasePoolError,
    ) -> None:
        with self._lock:
            if self._creations.get(key) is creation:
                del self._creations[key]
            creation.failure = failure
            creation.done.set()

    @staticmethod
    def _require_reusable_entry(entry: _PoolEntry | None) -> None:
        """Reject reuse while an abandoned engine still has active leases."""

        if entry is not None and entry.quarantined:
            raise DatabasePoolError("Database source is quarantined until active work completes")

    def _evict_idle_locked(self) -> Engine | None:
        reserved = len(self._entries) + len(self._creations)
        if reserved < self._settings.db_max_source_pools:
            return None
        candidates = [(key, entry) for key, entry in self._entries.items() if entry.leases == 0]
        if not candidates:
            raise DatabasePoolError("Active database sources reached PLANTAIN_DB_MAX_SOURCE_POOLS")
        key, entry = min(candidates, key=lambda item: item[1].last_used)
        del self._entries[key]
        return entry.engine

    def _create_entry(self, resolved: ResolvedDatabaseSource) -> _PoolEntry:
        options: dict[str, Any] = {
            "connect_args": dict(resolved.connect_args),
            "max_overflow": self._settings.db_max_overflow,
            "pool_pre_ping": True,
            "pool_recycle": self._settings.db_pool_recycle_seconds,
            "pool_reset_on_return": "rollback",
            "pool_size": self._settings.db_pool_size,
            "pool_timeout": self._settings.db_pool_timeout_seconds,
            "pool_use_lifo": True,
        }
        engine: Engine | None = None
        try:
            engine = create_engine(resolved.sqlalchemy_url, **options)
            _install_driver_timeout(engine, resolved.vendor, self._settings)
            install_agent_read_only_firewall(engine)
        except Exception as exc:
            if engine is not None:
                engine.dispose(close=True)
            raise DatabasePoolError(
                f"Unable to initialize the {resolved.vendor.value} database driver "
                f"({type(exc).__name__})"
            ) from exc
        return _PoolEntry(engine=engine, resolved=resolved)


def _install_driver_timeout(
    engine: Engine,
    vendor: DatabaseVendor,
    settings: Settings,
) -> None:
    if vendor is not DatabaseVendor.SQL_SERVER:
        return
    seconds = max(1, math.ceil(settings.db_query_timeout_seconds))

    @event.listens_for(engine, "before_cursor_execute")
    def set_sql_server_timeout(
        _connection: object,
        cursor: object,
        _statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if hasattr(cursor, "timeout"):
            cursor.timeout = seconds


def _validate_pool_settings(settings: Settings) -> None:
    bounds = (
        (settings.db_pool_size, 1, MAX_POOL_SIZE, "PLANTAIN_DB_POOL_SIZE"),
        (settings.db_max_overflow, 0, MAX_POOL_OVERFLOW, "PLANTAIN_DB_MAX_OVERFLOW"),
        (settings.db_max_source_pools, 1, MAX_SOURCE_POOLS, "PLANTAIN_DB_MAX_SOURCE_POOLS"),
        (
            settings.db_max_sources_per_scenario,
            1,
            MAX_SOURCES_PER_SCENARIO,
            "PLANTAIN_DB_MAX_SOURCES_PER_SCENARIO",
        ),
        (
            settings.db_fetch_batch_size,
            1,
            MAX_FETCH_BATCH_SIZE,
            "PLANTAIN_DB_FETCH_BATCH_SIZE",
        ),
    )
    for value, minimum, maximum, name in bounds:
        if value < minimum or value > maximum:
            raise DatabasePoolError(f"{name} must be between {minimum} and {maximum}")
    total_capacity = settings.db_max_source_pools * (
        settings.db_pool_size + settings.db_max_overflow
    )
    if total_capacity > MAX_TOTAL_POOL_CONNECTIONS:
        raise DatabasePoolError(
            f"Combined database pool capacity must not exceed "
            f"{MAX_TOTAL_POOL_CONNECTIONS} connections"
        )


__all__ = ["DatabaseLease", "DatabasePoolError", "DatabasePoolManager"]
