"""Direct, bounded scenario execution owned by the local dashboard."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import Literal

from plantain.config import Settings
from plantain.dashboard.reporting_profile import dashboard_reporting_runtime
from plantain.dashboard.run_progress import (
    DashboardRunProgress,
    DashboardRunProgressChannel,
)
from plantain.dashboard.scenario_catalog import (
    ScenarioCatalogError,
    is_valid_scenario_id,
    resolve_scenario_path,
)
from plantain.engine.admission import ResourceAdmission, ResourceKind
from plantain.engine.loader import load_scenario
from plantain.engine.progress import RunProgressObserver
from plantain.engine.runner import ScenarioRunner
from plantain.errors import (
    ActivityExecutionError,
    AtomicPersistenceError,
    ConfigurationError,
    PlantainError,
    ScenarioLoadError,
)
from plantain.models.scenario import ScenarioDefinition
from plantain.observability import configure_logging
from plantain.security.redaction import RedactionPolicy

MILLISECONDS_PER_SECOND = 1_000
MAX_RUN_NAME_LENGTH = 160
JOB_ID_BYTES = 16
JOB_ID_GENERATION_ATTEMPTS = 8
_DASHBOARD_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


class DashboardRunError(RuntimeError):
    """Raised when a dashboard run cannot safely start or finish."""


@dataclass(frozen=True, slots=True)
class DashboardRunOutcome:
    """Narrow browser-safe result of one explicitly requested test run."""

    scenario_id: str
    name: str
    status: Literal["passed", "failed"]
    duration_ms: int
    completed_steps: int
    correlation_id: str
    message: str


@dataclass(frozen=True, slots=True)
class DashboardRunHandle:
    """Opaque public identity for one backend-owned scenario task."""

    job_id: str
    scenario_id: str
    _task: asyncio.Task[DashboardRunOutcome] = field(repr=False)
    _progress: DashboardRunProgressChannel = field(repr=False)

    async def wait(self) -> DashboardRunOutcome:
        """Wait for the backend-owned task without exposing it."""

        return await self._task

    async def next_progress(self) -> DashboardRunProgress:
        """Wait for the next coalesced value-free lifecycle snapshot."""

        return await self._progress.next()


@dataclass(frozen=True, slots=True)
class _RuntimeIdentity:
    project_root: Path
    output_dir: Path
    log_level: str
    sensitive_key_names: tuple[str, ...]
    resource_limits: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ExecutionRuntime:
    identity: _RuntimeIdentity
    admission: ResourceAdmission


class _RuntimeOwner:
    """Own the process-wide dashboard admission controller and logging setup."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._runtime: _ExecutionRuntime | None = None

    def get_or_create(self, settings: Settings) -> _ExecutionRuntime:
        identity = _runtime_identity(settings)
        with self._lock:
            if self._runtime is not None:
                if self._runtime.identity != identity:
                    raise DashboardRunError(
                        "Dashboard runtime configuration changed; restart Plantain to apply it"
                    )
                return self._runtime
            runtime = _initialize_runtime(settings, identity)
            self._runtime = runtime
            return runtime


@dataclass(frozen=True, slots=True)
class _DashboardJob:
    task: asyncio.Task[DashboardRunOutcome]
    loop: asyncio.AbstractEventLoop


class _RunCoordinator:
    """Own cancellable dashboard tasks without serializing scenario execution."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: dict[str, _DashboardJob] = {}

    def start(self, project_root: Path, scenario_id: str) -> DashboardRunHandle:
        loop = asyncio.get_running_loop()
        with self._lock:
            job_id = self._new_job_id()
            progress = DashboardRunProgressChannel()
            task = loop.create_task(
                run_dashboard_scenario(
                    project_root,
                    scenario_id,
                    progress_observer=progress.observe,
                ),
                name=f"plantain-dashboard-run-{job_id}",
            )
            self._jobs[job_id] = _DashboardJob(task=task, loop=loop)

        def retire(completed: asyncio.Task[DashboardRunOutcome]) -> None:
            self._retire(job_id, completed)

        task.add_done_callback(retire)
        return DashboardRunHandle(
            job_id=job_id,
            scenario_id=scenario_id,
            _task=task,
            _progress=progress,
        )

    def cancel(self, job_id: str) -> bool:
        if not is_valid_dashboard_job_id(job_id):
            return False
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.task.done():
                return False
            try:
                job.loop.call_soon_threadsafe(job.task.cancel)
            except RuntimeError:
                return False
            return True

    def _new_job_id(self) -> str:
        for _attempt in range(JOB_ID_GENERATION_ATTEMPTS):
            candidate = secrets.token_hex(JOB_ID_BYTES)
            if candidate not in self._jobs:
                return candidate
        raise DashboardRunError("Plantain could not allocate a test run safely")

    def _retire(
        self,
        job_id: str,
        completed: asyncio.Task[DashboardRunOutcome],
    ) -> None:
        with suppress(asyncio.CancelledError):
            completed.exception()
        with self._lock:
            current = self._jobs.get(job_id)
            if current is not None and current.task is completed:
                self._jobs.pop(job_id, None)


_runtime_owner = _RuntimeOwner()
_run_coordinator = _RunCoordinator()


def is_valid_dashboard_job_id(value: object) -> bool:
    """Return whether a browser value can identify a dashboard-owned job."""

    return isinstance(value, str) and _DASHBOARD_JOB_ID.fullmatch(value) is not None


def dashboard_run_capacity(project_root: Path) -> int:
    """Return the process-wide scenario capacity used by dashboard jobs."""

    settings = _runtime_settings(project_root)
    runtime = _runtime_for(settings)
    return runtime.admission.limit(ResourceKind.SCENARIO)


def start_dashboard_run(
    project_root: Path,
    scenario_id: str,
) -> DashboardRunHandle:
    """Start one scenario task without weakening engine admission limits."""

    if not is_valid_scenario_id(scenario_id):
        raise DashboardRunError("The selected test identifier is invalid")
    try:
        return _run_coordinator.start(project_root, scenario_id)
    except DashboardRunError:
        raise
    except RuntimeError as exc:
        raise DashboardRunError("Plantain could not start the selected test safely") from exc


async def wait_dashboard_run(handle: DashboardRunHandle) -> DashboardRunOutcome:
    """Wait for a backend-owned run through its non-serializable handle."""

    return await handle.wait()


async def wait_dashboard_run_progress(
    handle: DashboardRunHandle,
) -> DashboardRunProgress:
    """Wait for a backend-owned run's next coalesced progress projection."""

    return await handle.next_progress()


async def monitor_dashboard_run(
    handle: DashboardRunHandle,
    update: Callable[[DashboardRunProgress], Awaitable[None]],
) -> DashboardRunOutcome:
    """Follow value-free progress while independently awaiting the final outcome."""

    outcome_task = asyncio.create_task(handle.wait())
    progress_task = asyncio.create_task(handle.next_progress())
    try:
        while True:
            completed, _pending = await asyncio.wait(
                (outcome_task, progress_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if progress_task in completed:
                try:
                    await update(progress_task.result())
                except Exception:  # noqa: BLE001 - live projection cannot fail execution.
                    return await outcome_task
            if outcome_task in completed:
                return outcome_task.result()
            progress_task = asyncio.create_task(handle.next_progress())
    finally:
        if not progress_task.done():
            progress_task.cancel()
        if not outcome_task.done():
            outcome_task.cancel()
        await asyncio.gather(progress_task, outcome_task, return_exceptions=True)


def cancel_dashboard_run(job_id: str) -> bool:
    """Request cooperative cancellation for one active dashboard job."""

    return _run_coordinator.cancel(job_id)


async def run_dashboard_scenario(
    project_root: Path,
    scenario_id: str,
    *,
    progress_observer: RunProgressObserver | None = None,
) -> DashboardRunOutcome:
    """Validate and execute one catalog scenario without invoking the CLI."""

    base_settings = await asyncio.to_thread(_runtime_settings, project_root)
    reporting = await asyncio.to_thread(
        dashboard_reporting_runtime,
        base_settings,
    )
    settings = reporting.settings
    runtime = await asyncio.to_thread(_runtime_for, settings)
    try:
        path = await asyncio.to_thread(
            resolve_scenario_path,
            settings.project_root,
            scenario_id,
        )
        scenario = await asyncio.to_thread(load_scenario, path, settings)
    except (ScenarioCatalogError, ScenarioLoadError) as exc:
        raise DashboardRunError(
            "The selected test changed and must be reviewed before it can run"
        ) from exc

    try:
        runner = ScenarioRunner(
            settings,
            admission=runtime.admission,
            progress_observer=progress_observer,
            reporting_environ=reporting.environment,
        )
        _validate_before_execution(runner, scenario)
    except PlantainError as exc:
        raise DashboardRunError("The selected test must be reviewed before it can run") from exc
    except Exception as exc:
        raise DashboardRunError("The selected test could not be prepared safely") from exc

    redaction = RedactionPolicy(settings.sensitive_key_names)
    name = _safe_run_name(scenario.scenario, redaction)
    started = time.monotonic()
    try:
        result = await runner.run(scenario)
    except asyncio.CancelledError:
        raise
    except (ActivityExecutionError, AtomicPersistenceError, OSError):
        return DashboardRunOutcome(
            scenario_id=scenario_id,
            name=name,
            status="failed",
            duration_ms=_duration_ms(started),
            completed_steps=0,
            correlation_id="",
            message="Test failed. Review its locally stored run evidence for details.",
        )
    except Exception as exc:
        raise DashboardRunError("The test could not be completed safely") from exc

    return DashboardRunOutcome(
        scenario_id=scenario_id,
        name=_safe_run_name(result.scenario, redaction),
        status="passed",
        duration_ms=result.duration_ms,
        completed_steps=len(result.steps),
        correlation_id=result.correlation_id,
        message=(f"{len(result.steps)} {'step' if len(result.steps) == 1 else 'steps'} completed."),
    )


def _runtime_settings(project_root: Path) -> Settings:
    root = _resolve_project_root(project_root)
    try:
        settings = replace(
            Settings.from_env(root),
            allow_db_mutations=False,
        )
        settings.ensure_runtime_directories()
    except (AtomicPersistenceError, ConfigurationError, OSError) as exc:
        raise DashboardRunError("The dashboard runtime configuration is unavailable") from exc
    return settings


def _resolve_project_root(project_root: Path) -> Path:
    try:
        root = project_root.resolve(strict=True)
    except OSError as exc:
        raise DashboardRunError("The dashboard workspace is unavailable") from exc
    if not root.is_dir():
        raise DashboardRunError("The dashboard workspace is unavailable")
    return root


def _initialize_runtime(
    settings: Settings,
    identity: _RuntimeIdentity,
) -> _ExecutionRuntime:
    try:
        configure_logging(
            settings.output_dir,
            settings.log_level,
            sensitive_keys=settings.sensitive_key_names,
        )
        admission = ResourceAdmission(settings)
    except (AtomicPersistenceError, ConfigurationError, OSError) as exc:
        raise DashboardRunError("The dashboard runtime could not be initialized safely") from exc
    return _ExecutionRuntime(identity=identity, admission=admission)


def _runtime_for(settings: Settings) -> _ExecutionRuntime:
    return _runtime_owner.get_or_create(settings)


def _runtime_identity(settings: Settings) -> _RuntimeIdentity:
    return _RuntimeIdentity(
        project_root=settings.project_root,
        output_dir=settings.output_dir,
        log_level=settings.log_level,
        sensitive_key_names=settings.sensitive_key_names,
        resource_limits=(
            settings.max_scenario_concurrency,
            settings.max_browser_sessions,
            settings.max_api_requests,
            settings.max_database_operations,
            settings.max_worker_threads,
            settings.max_worker_processes,
        ),
    )


def _validate_before_execution(
    runner: ScenarioRunner,
    scenario: ScenarioDefinition,
) -> None:
    runner.validate_scenario(scenario)


def _safe_run_name(value: str, redaction: RedactionPolicy) -> str:
    rendered = " ".join(redaction.redact_text(value).split())
    if len(rendered) <= MAX_RUN_NAME_LENGTH:
        return rendered
    return f"{rendered[: MAX_RUN_NAME_LENGTH - 1]}…"


def _duration_ms(started: float) -> int:
    return round((time.monotonic() - started) * MILLISECONDS_PER_SECOND)


__all__ = [
    "DashboardRunError",
    "DashboardRunHandle",
    "DashboardRunOutcome",
    "cancel_dashboard_run",
    "dashboard_run_capacity",
    "is_valid_dashboard_job_id",
    "monitor_dashboard_run",
    "run_dashboard_scenario",
    "start_dashboard_run",
    "wait_dashboard_run",
    "wait_dashboard_run_progress",
]
