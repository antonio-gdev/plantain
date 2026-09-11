"""Lazy runtime services, ownership, cleanup, and operation evidence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities import snapshot_registry_complete, ui_browser, ui_telemetry
from plantain.activities.api import client as api_client
from plantain.activities.api import openapi
from plantain.activities.database import pool as database_pool
from plantain.activities.database import session as database_session
from plantain.config import Settings
from plantain.engine import runtime
from plantain.engine.context import ScenarioContext
from plantain.engine.progress import RunProgressEvent
from plantain.engine.runtime import ExecutionServices, RunContext
from plantain.models.scenario import ScenarioDefinition, StepDefinition
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry


def _settings(tmp_path: Path) -> Settings:
    return cast(
        "Settings",
        SimpleNamespace(
            allow_private_networks=False,
            allowed_hosts=("example.test",),
            browser="chromium",
            headless=True,
            slow_mo_ms=0,
            action_timeout_ms=1_000,
            navigation_timeout_ms=2_000,
            viewport_width=1_280,
            viewport_height=720,
            storage_state_path=None,
            trace_mode="off",
            output_dir=tmp_path / "output",
            snapshots_dir=tmp_path / "snapshots",
            browser_auto_install=False,
            browser_install_timeout_seconds=45.0,
            allow_custom_playwright_download_hosts=False,
        ),
    )


def _scenario(name: str) -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario=name,
        steps=[StepDefinition(activity="probe", params={"id": "probe"})],
    )


def test_services_reject_conflicting_database_pool_sources(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        ExecutionServices(
            _settings(tmp_path),
            SecretRegistry(),
            cast("Any", object()),
            database_pool_factory=lambda: cast("Any", object()),
        )


def test_services_lazily_create_reuse_and_close_owned_integrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Telemetry:
        def __init__(
            self,
            secrets: SecretRegistry,
            **options: Any,
        ) -> None:
            self.secrets = secrets
            self.options = options

    class Browser:
        def __init__(
            self,
            options: Any,
            validate: Any,
            validate_websocket: Any,
            telemetry: Telemetry,
            *,
            admission: Any,
        ) -> None:
            self.options = options
            self.validate = validate
            self.validate_websocket = validate_websocket
            self.telemetry = telemetry
            self.admission = admission

        async def close(self, *, failed: bool) -> None:
            events.append(f"browser:{failed}")

    class Api:
        def __init__(
            self,
            settings: Settings,
            policy: Any,
            *,
            admission: Any,
        ) -> None:
            self.settings = settings
            self.policy = policy
            self.admission = admission

        async def close(self) -> None:
            events.append("api")

    class SchemaStore:
        def __init__(
            self,
            settings: Settings,
            api: Api,
            *,
            admission: Any,
        ) -> None:
            self.settings = settings
            self.api = api
            self.admission = admission

    class Pools:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        def close(self) -> None:
            events.append("pools")

    class Database:
        def __init__(
            self,
            settings: Settings,
            pools: Pools,
            *,
            admission: Any,
        ) -> None:
            self.settings = settings
            self.pools = pools
            self.admission = admission

        async def close(self) -> None:
            events.append("database")

    class Registry:
        def __init__(self, snapshots_dir: Path) -> None:
            self.snapshots_dir = snapshots_dir

    monkeypatch.setattr(ui_telemetry, "NetworkTelemetryRecorder", Telemetry)
    monkeypatch.setattr(ui_browser, "BrowserSession", Browser)
    monkeypatch.setattr(api_client, "ApiSession", Api)
    monkeypatch.setattr(openapi, "OpenApiStore", SchemaStore)
    monkeypatch.setattr(database_pool, "DatabasePoolManager", Pools)
    monkeypatch.setattr(database_session, "DatabaseSession", Database)
    monkeypatch.setattr(snapshot_registry_complete, "CompleteSnapshotRegistry", Registry)
    secrets = SecretRegistry()
    services = ExecutionServices(_settings(tmp_path), secrets)

    async def exercise() -> tuple[Any, Any, Any, Any]:
        browser = await services.browser()
        assert await services.browser() is browser
        api = await services.api()
        assert await services.api() is api
        schema_store = await services.openapi_store()
        assert await services.openapi_store() is schema_store
        database = await services.database()
        assert await services.database() is database
        registry = await services.snapshot_registry()
        assert await services.snapshot_registry() is registry
        await services.close(failed=True)
        return browser, schema_store, database, registry

    browser, schema_store, database, registry = asyncio.run(exercise())

    assert browser.options.browser == "chromium"
    assert browser.options.auto_install is False
    assert browser.telemetry.secrets is secrets
    assert schema_store.api is services._api
    assert database.pools is services._database_pools
    assert registry.snapshots_dir == tmp_path / "snapshots"
    assert events == ["database", "api", "browser:True", "pools"]


@pytest.mark.parametrize("use_factory", [False, True])
def test_shared_or_factory_database_pools_are_not_closed_by_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    use_factory: bool,
) -> None:
    pool_close_calls = 0
    factory_calls = 0

    class Pools:
        def close(self) -> None:
            nonlocal pool_close_calls
            pool_close_calls += 1

    # This fake mirrors the runtime's keyword-only admission injection contract.
    class Database:
        def __init__(
            self,
            _settings: Settings,
            pools: Pools,
            *,
            admission: Any,
        ) -> None:
            self.pools = pools
            self.admission = admission

        async def close(self) -> None:
            pass

    pools = Pools()

    def factory() -> Pools:
        nonlocal factory_calls
        factory_calls += 1
        return pools

    monkeypatch.setattr(database_session, "DatabaseSession", Database)
    services = ExecutionServices(
        _settings(tmp_path),
        SecretRegistry(),
        None if use_factory else cast("Any", pools),
        database_pool_factory=cast("Any", factory) if use_factory else None,
    )

    async def exercise() -> Any:
        database = await services.database()
        await services.close(failed=False)
        return database

    database = asyncio.run(exercise())

    assert database.pools is pools
    assert factory_calls == int(use_factory)
    assert pool_close_calls == 0


def test_service_cleanup_attempts_every_resource_and_aggregates_failures(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Database:
        async def close(self) -> None:
            events.append("database")
            raise RuntimeError("database close rejected")

    class Api:
        async def close(self) -> None:
            events.append("api")
            raise RuntimeError("api close rejected")

    class Browser:
        async def close(self, *, failed: bool) -> None:
            events.append(f"browser:{failed}")
            raise RuntimeError("browser close rejected")

    class Pools:
        def close(self) -> None:
            events.append("pools")
            raise RuntimeError("pool close rejected")

    services = ExecutionServices(_settings(tmp_path), SecretRegistry())
    services._database = cast("Any", Database())
    services._api = cast("Any", Api())
    services._browser = cast("Any", Browser())
    services._database_pools = cast("Any", Pools())

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(services.close(failed=True))

    expected_events = ["database", "api", "browser:True", "pools"]
    assert len(raised.value.exceptions) == len(expected_events)
    assert events == expected_events


def test_run_context_retains_only_sanitized_operation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_value = "synthetic-sensitive-value"
    progress_events: list[RunProgressEvent] = []
    secrets = SecretRegistry(sensitive_keys=("password",))
    context = RunContext(
        scenario=_scenario("Runtime evidence"),
        settings=_settings(tmp_path),
        values=ScenarioContext(),
        services=cast("ExecutionServices", object()),
        secrets=secrets,
        correlation_id="run-1",
        current_activity="sendRequest",
        current_step_id="request",
        progress_observer=progress_events.append,
    )
    monkeypatch.setattr(runtime.time, "time_ns", lambda: 1_000_000_000)

    context.add_artifact(kind="report", path="output/result.json", description="Result")
    context.add_operation(
        domain="api",
        phase="request",
        operation_type="POST",
        target="https://example.test/items",
        status="failed",
        duration_ms=25,
        position=1,
        total=2,
        operation_input={"password": observed_value},
        operation_expected={"statusCode": [201]},
        operation_actual={"httpStatus": 500},
        error_type="ApiActivityError",
    )

    assert context.artifacts == [
        {"kind": "report", "path": "output/result.json", "description": "Result"}
    ]
    assert context.operations == [
        {
            "domain": "api",
            "phase": "request",
            "activity": "sendRequest",
            "step_id": "request",
            "operation_type": "POST",
            "operation_target": "https://example.test/items",
            "status": "failed",
            "duration_ms": 25,
            "started_at_ms": 975,
            "stopped_at_ms": 1_000,
            "operation_index": 1,
            "operation_total": 2,
            "operation_input": {"password": REDACTED},
            "operation_expected": {"statusCode": [201]},
            "operation_actual": {"httpStatus": 500},
            "operation_error_type": "ApiActivityError",
        }
    ]
    assert len(progress_events) == 1
    progress = progress_events[0]
    assert progress.kind == "operation"
    assert progress.status == "failed"
    assert progress.domain == "api"
    assert progress.operation_phase == "request"
    assert progress.operation_type == "POST"
    assert not hasattr(progress, "target")
    assert observed_value not in repr(progress)

    def reject_progress(_event: RunProgressEvent) -> None:
        raise RuntimeError(observed_value)

    context.progress_observer = reject_progress
    context.add_operation(
        domain="ui",
        phase="verification",
        operation_type="visible",
        target="css:#private-target",
        status="passed",
        duration_ms=1,
    )
    assert context.operations[-1]["status"] == "passed"


def test_run_context_rejects_non_mapping_sanitizer_results(tmp_path: Path) -> None:
    class InvalidSecrets:
        def redact(self, _value: object) -> list[object]:
            return []

    context = RunContext(
        scenario=_scenario("Invalid sanitizer"),
        settings=_settings(tmp_path),
        values=ScenarioContext(),
        services=cast("ExecutionServices", object()),
        secrets=cast("SecretRegistry", InvalidSecrets()),
    )

    with pytest.raises(TypeError, match="sanitization produced an invalid result"):
        context.add_operation(
            domain="ui",
            phase="action",
            operation_type="click",
            target="css:#continue",
            status="passed",
            duration_ms=1,
        )
