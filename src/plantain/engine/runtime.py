"""Scenario runtime state and lazy integration service lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from plantain.config import Settings
from plantain.engine.admission import ResourceAdmission
from plantain.engine.context import ScenarioContext
from plantain.engine.progress import (
    RunProgressEvent,
    RunProgressObserver,
    RunProgressStatus,
)
from plantain.models.scenario import ScenarioDefinition
from plantain.security.secrets import SecretRegistry
from plantain.security.url_policy import UrlPolicy

if TYPE_CHECKING:
    from plantain.activities.api.client import ApiSession
    from plantain.activities.api.openapi import OpenApiStore
    from plantain.activities.database.pool import DatabasePoolManager
    from plantain.activities.database.session import DatabaseSession
    from plantain.activities.snapshot_registry_complete import CompleteSnapshotRegistry
    from plantain.activities.ui_browser import BrowserSession


class ExecutionServices:
    """Owns lazily initialized browser, HTTP, database, and registry resources."""

    def __init__(
        self,
        settings: Settings,
        secrets: SecretRegistry,
        database_pools: DatabasePoolManager | None = None,
        *,
        database_pool_factory: Callable[[], DatabasePoolManager] | None = None,
        admission: ResourceAdmission | None = None,
    ) -> None:
        if database_pools is not None and database_pool_factory is not None:
            raise ValueError("database_pools and database_pool_factory are mutually exclusive")
        self.settings = settings
        self._secrets = secrets
        self._database_pools = database_pools
        self._database_pool_factory = database_pool_factory
        self._owns_database_pools = database_pools is None and database_pool_factory is None
        self._admission = admission or ResourceAdmission(settings)
        self.url_policy = UrlPolicy(
            environment=getattr(settings, "environment", "production"),
            network_mode=getattr(settings, "network_mode", "standard"),
            allow_private_networks=settings.allow_private_networks,
            allowed_hosts=settings.allowed_hosts,
            blocked_hosts=getattr(settings, "blocked_hosts", ()),
            allow_insecure_local_http=getattr(settings, "allow_insecure_local_http", False),
            egress_control_enforced=getattr(settings, "egress_control_enforced", False),
            admission=self._admission,
        )
        self._browser: BrowserSession | None = None
        self._api: ApiSession | None = None
        self._openapi_store: OpenApiStore | None = None
        self._database: DatabaseSession | None = None
        self._snapshot_registry: CompleteSnapshotRegistry | None = None

    @property
    def admission(self) -> ResourceAdmission:
        return self._admission

    async def browser(self) -> BrowserSession:
        if self._browser is None:
            from plantain.activities.ui_browser import (  # noqa: PLC0415
                BrowserOptions,
                BrowserSession,
            )
            from plantain.activities.ui_telemetry import (  # noqa: PLC0415
                DEFAULT_MAX_REQUEST_CORRELATIONS,
                DEFAULT_MAX_SPOOL_BYTES,
                NetworkTelemetryRecorder,
            )

            self._browser = BrowserSession(
                BrowserOptions(
                    browser=self.settings.browser,  # type: ignore[arg-type]
                    headless=self.settings.headless,
                    slow_mo_ms=self.settings.slow_mo_ms,
                    timeout_ms=self.settings.action_timeout_ms,
                    navigation_timeout_ms=self.settings.navigation_timeout_ms,
                    viewport_width=self.settings.viewport_width,
                    viewport_height=self.settings.viewport_height,
                    storage_state=self.settings.storage_state_path,
                    trace_mode=self.settings.trace_mode,  # type: ignore[arg-type]
                    trace_dir=self.settings.output_dir / "traces",
                    trace_data_governance_approved=getattr(
                        self.settings,
                        "trace_data_governance_approved",
                        False,
                    ),
                    trace_storage_encrypted=getattr(
                        self.settings,
                        "trace_storage_encrypted",
                        False,
                    ),
                    trace_retention_days=getattr(self.settings, "trace_retention_days", 7),
                    trace_max_archives=getattr(self.settings, "trace_max_archives", 20),
                    trace_max_bytes=getattr(
                        self.settings,
                        "trace_max_bytes",
                        1_073_741_824,
                    ),
                    auto_install=getattr(self.settings, "browser_auto_install", True),
                    install_timeout_seconds=getattr(
                        self.settings,
                        "browser_install_timeout_seconds",
                        300.0,
                    ),
                    allow_custom_download_hosts=getattr(
                        self.settings,
                        "allow_custom_playwright_download_hosts",
                        False,
                    ),
                    strict_network_policy=(
                        getattr(self.settings, "network_mode", "standard") == "restricted"
                    ),
                ),
                self.url_policy.validate,
                self.url_policy.validate_websocket,
                NetworkTelemetryRecorder(
                    self._secrets,
                    max_spool_bytes=getattr(
                        self.settings,
                        "snapshot_max_capture_bytes",
                        DEFAULT_MAX_SPOOL_BYTES,
                    ),
                    max_request_correlations=getattr(
                        self.settings,
                        "snapshot_max_network_correlations",
                        DEFAULT_MAX_REQUEST_CORRELATIONS,
                    ),
                ),
                admission=self._admission,
            )
        return self._browser

    async def api(self) -> ApiSession:
        if self._api is None:
            from plantain.activities.api.client import ApiSession  # noqa: PLC0415

            self._api = ApiSession(
                self.settings,
                self.url_policy,
                admission=self._admission,
            )
        return self._api

    async def openapi_store(self) -> OpenApiStore:
        if self._openapi_store is None:
            from plantain.activities.api.openapi import OpenApiStore  # noqa: PLC0415

            self._openapi_store = OpenApiStore(
                self.settings,
                await self.api(),
                admission=self._admission,
            )
        return self._openapi_store

    async def database(self) -> DatabaseSession:
        if self._database is None:
            from plantain.activities.database.pool import DatabasePoolManager  # noqa: PLC0415
            from plantain.activities.database.session import DatabaseSession  # noqa: PLC0415

            if self._database_pools is None:
                if self._database_pool_factory is None:
                    self._database_pools = DatabasePoolManager(self.settings)
                else:
                    self._database_pools = self._database_pool_factory()
            self._database = DatabaseSession(
                self.settings,
                self._database_pools,
                admission=self._admission,
            )
        return self._database

    async def snapshot_registry(self) -> CompleteSnapshotRegistry:
        if self._snapshot_registry is None:
            from plantain.activities.snapshot_registry_complete import (  # noqa: PLC0415
                CompleteSnapshotRegistry,
            )

            self._snapshot_registry = CompleteSnapshotRegistry(self.settings.snapshots_dir)
        return self._snapshot_registry

    async def close(self, *, failed: bool) -> None:
        errors: list[Exception] = []
        if self._database is not None:
            try:
                await self._database.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must aggregate all failures.
                errors.append(exc)
        if self._api is not None:
            try:
                await self._api.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must aggregate all failures.
                errors.append(exc)
        if self._browser is not None:
            try:
                await self._browser.close(failed=failed)
            except Exception as exc:  # noqa: BLE001 - cleanup must aggregate all failures.
                errors.append(exc)
        if self._owns_database_pools and self._database_pools is not None:
            try:
                await self._admission.run_blocking(self._database_pools.close)
            except Exception as exc:  # noqa: BLE001 - cleanup must aggregate all failures.
                errors.append(exc)
        if errors:
            raise ExceptionGroup("One or more runtime resources failed to close", errors)


@dataclass(slots=True)
class RunContext:
    """All state explicitly available to an activity handler."""

    scenario: ScenarioDefinition
    settings: Settings
    values: ScenarioContext
    services: ExecutionServices
    secrets: SecretRegistry
    correlation_id: str = ""
    current_activity: str | None = None
    current_step_id: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    progress_observer: RunProgressObserver | None = field(default=None, repr=False)

    def emit_progress(self, event: RunProgressEvent) -> None:
        """Notify an optional observer without changing execution semantics."""

        if self.progress_observer is None:
            return
        with suppress(Exception):
            self.progress_observer(event)

    def add_artifact(self, *, kind: str, path: str, description: str) -> None:
        self.artifacts.append({"kind": kind, "path": path, "description": description})

    def add_operation(
        self,
        *,
        domain: str,
        phase: str,
        operation_type: str,
        target: str,
        status: str,
        duration_ms: int,
        position: int | None = None,
        total: int | None = None,
        operation_input: object | None = None,
        operation_expected: object | None = None,
        operation_actual: object | None = None,
        error_type: str | None = None,
    ) -> None:
        """Retain one bounded, sanitized terminal operation for result reporting."""

        operation: dict[str, object] = {
            "domain": domain,
            "phase": phase,
            "activity": self.current_activity or "scenario",
            "step_id": self.current_step_id or "scenario",
            "operation_type": operation_type,
            "operation_target": target,
            "status": status,
            "duration_ms": duration_ms,
        }
        stopped_at_ms = time.time_ns() // 1_000_000
        operation["started_at_ms"] = max(0, stopped_at_ms - duration_ms)
        operation["stopped_at_ms"] = stopped_at_ms
        optional = {
            "operation_index": position,
            "operation_total": total,
            "operation_input": operation_input,
            "operation_expected": operation_expected,
            "operation_actual": operation_actual,
            "operation_error_type": error_type,
        }
        operation.update({key: value for key, value in optional.items() if value is not None})
        sanitized = self.secrets.redact(operation)
        if not isinstance(sanitized, dict):
            raise TypeError("Scenario operation sanitization produced an invalid result")
        self.operations.append(sanitized)
        if status not in {"passed", "failed"}:
            return
        progress_status: RunProgressStatus = "passed" if status == "passed" else "failed"
        self.emit_progress(
            RunProgressEvent(
                correlation_id=self.correlation_id,
                kind="operation",
                status=progress_status,
                total_steps=len(self.scenario.steps),
                activity=str(sanitized.get("activity", "")),
                step_id=str(sanitized.get("step_id", "")),
                domain=str(sanitized.get("domain", "")),
                operation_phase=str(sanitized.get("phase", "")),
                operation_type=str(sanitized.get("operation_type", "")),
                operation_position=position,
                operation_total=total,
                duration_ms=duration_ms,
                error_type=str(sanitized.get("operation_error_type", "")),
            )
        )


# Sanitized operation evidence remains ordered with scenario execution.
