"""Asynchronous, resource-safe scenario execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Never, cast
from uuid import uuid4

from pydantic import BaseModel

from plantain.activities import register_framework_activities
from plantain.activities.database.pool import DatabasePoolManager
from plantain.config import Settings
from plantain.engine.admission import ResourceAdmission, ResourceKind
from plantain.engine.context import ScenarioContext
from plantain.engine.expressions import ExpressionResolver
from plantain.engine.loader import MAX_DISCOVERED_SCENARIO_FILES, load_scenario
from plantain.engine.progress import (
    RunProgressEvent,
    RunProgressObserver,
    RunProgressStatus,
)
from plantain.engine.registry import ActivityRegistry
from plantain.engine.report_store import scenario_report_target
from plantain.engine.reporting import ScenarioReporting
from plantain.engine.runtime import ExecutionServices, RunContext
from plantain.errors import ActivityExecutionError, StepFailure
from plantain.models.scenario import ScenarioDefinition
from plantain.observability import (
    bind_log_secrets,
    emit_scenario_completed,
    get_logger,
    reset_log_secrets,
)
from plantain.persistence import write_json_atomic
from plantain.reporting import ResultReporter
from plantain.reporting.allure import write_allure_result
from plantain.security.redaction import RedactionPolicy
from plantain.security.secrets import SecretRegistry

logger = get_logger("runner")
ALLURE_PERSISTENCE_FAILURE = "Optional Allure result persistence failed"
REPORT_PERSISTENCE_FAILURE = (
    "Scenario report persistence failed on the configured output filesystem"
)
TELEMETRY_PERSISTENCE_FAILURE = (
    "Scenario telemetry persistence failed on the configured output filesystem"
)
RUNTIME_CLEANUP_TIMEOUT = "Runtime resource cleanup exceeded the configured deadline"
REPORTER_CLEANUP_TIMEOUT = "Result reporter cleanup exceeded the configured deadline"


@dataclass(frozen=True, slots=True)
class StepResult:
    activity: str
    step_id: str
    status: str
    duration_ms: int


@dataclass(slots=True)
class ScenarioResult:
    scenario: str
    status: str
    duration_ms: int
    started_at_ms: int = 0
    correlation_id: str = ""
    source_path: str | None = None
    jira_ticket: str | None = None
    test_case_key: str | None = None
    test_run_key: str | int | None = None
    tags: list[str] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    failure: StepFailure | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    operations: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    integrations: dict[str, dict[str, str | int | None]] = field(default_factory=dict)
    _sensitive_key_names: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def sanitized_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        sensitive_keys = tuple(payload.pop("_sensitive_key_names", ()))
        return cast(
            "dict[str, Any]",
            RedactionPolicy(sensitive_keys).redact_artifact(payload),
        )


@dataclass(slots=True)
class _FailureState:
    """Preserve the first scenario failure while recording secondary failures safely."""

    error: Exception | None = None
    message: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def record(
        self,
        *,
        error: Exception,
        message: str,
        result: ScenarioResult,
        activity: str,
        step_id: str,
    ) -> None:
        result.status = "failed"
        if self.error is not None:
            return
        self.error = error
        self.message = message
        if result.failure is None:
            result.failure = StepFailure(
                activity=activity,
                step_id=step_id,
                message=message,
                exception_type=type(error).__name__,
            )


@dataclass(slots=True)
class _FinalizationState:
    """Preserve the first required projection failure without changing test outcome."""

    error: Exception | None = None
    message: str | None = None

    def record(self, *, error: Exception, message: str) -> None:
        if self.error is None:
            self.error = error
            self.message = message


def _terminal_progress_status(
    failure_state: _FailureState,
    finalization_state: _FinalizationState,
    cancellation: asyncio.CancelledError | None,
) -> RunProgressStatus:
    if cancellation is not None:
        return "cancelled"
    if failure_state.error is not None or finalization_state.error is not None:
        return "failed"
    return "passed"


class ScenarioRunner:
    """Run validated YAML scenarios against a typed activity registry."""

    def __init__(
        self,
        settings: Settings,
        registry: ActivityRegistry | None = None,
        *,
        database_pools: DatabasePoolManager | None = None,
        database_pool_factory: Callable[[], DatabasePoolManager] | None = None,
        result_reporter: ResultReporter | None = None,
        reporting_environ: Mapping[str, str] | None = None,
        admission: ResourceAdmission | None = None,
        progress_observer: RunProgressObserver | None = None,
    ) -> None:
        if database_pools is not None and database_pool_factory is not None:
            raise ValueError("database_pools and database_pool_factory are mutually exclusive")
        self.settings = settings
        self.registry = registry or ActivityRegistry()
        self._database_pools: DatabasePoolManager | None = database_pools
        self._database_pool_factory = database_pool_factory
        self._owns_database_pools = database_pools is None and database_pool_factory is None
        self._admission = admission or ResourceAdmission(settings)
        self._progress_observer = progress_observer
        self._reporting = ScenarioReporting(
            settings,
            result_reporter,
            admission=self._admission,
            environ=reporting_environ,
        )
        self._batch_active = False
        if registry is None:
            register_framework_activities(self.registry)

    def _active_database_pools(self) -> DatabasePoolManager:
        if self._database_pools is None:
            if self._database_pool_factory is not None:
                pools = self._database_pool_factory()
                if pools.closed:
                    raise ActivityExecutionError("Shared database pool manager is closed")
                return pools
            if not self._owns_database_pools:
                raise ActivityExecutionError("Shared database pool manager is unavailable")
            self._database_pools = DatabasePoolManager(self.settings)
        elif self._database_pools.closed:
            if not self._owns_database_pools:
                raise ActivityExecutionError("Shared database pool manager is closed")
            self._database_pools = DatabasePoolManager(self.settings)
        return self._database_pools

    async def run_file(self, path: Path) -> ScenarioResult:
        return await self.run(load_scenario(path, self.settings))

    def validate_reporting(self, scenario: ScenarioDefinition) -> None:
        """Validate enabled reporting metadata without opening a network connection."""

        self._reporting.validate(scenario)

    def validate_scenario(self, scenario: ScenarioDefinition) -> None:
        """Validate reporting and every typed activity without executing a target."""

        self.validate_reporting(scenario)
        for step in scenario.steps:
            self.registry.validate(step.activity, step.params)

    async def run(self, scenario: ScenarioDefinition) -> ScenarioResult:
        started = time.monotonic()
        started_at_ms = time.time_ns() // 1_000_000
        correlation_id = uuid4().hex
        values = ScenarioContext()
        secrets = SecretRegistry(sensitive_keys=self.settings.sensitive_key_names)
        services = ExecutionServices(
            self.settings,
            secrets,
            None if self._owns_database_pools else self._database_pools,
            database_pool_factory=self._database_pool_factory,
            admission=self._admission,
        )
        context = RunContext(
            scenario=scenario,
            settings=self.settings,
            values=values,
            services=services,
            secrets=secrets,
            correlation_id=correlation_id,
            progress_observer=self._progress_observer,
        )
        result = ScenarioResult(
            scenario=scenario.scenario,
            status="running",
            duration_ms=0,
            started_at_ms=started_at_ms,
            correlation_id=correlation_id,
            source_path=scenario.source_path,
            jira_ticket=scenario.jira_ticket,
            test_case_key=scenario.test_case_key,
            test_run_key=scenario.test_run_key,
            tags=list(scenario.tags),
            _sensitive_key_names=self.settings.sensitive_key_names,
        )
        failure_state = _FailureState()
        finalization_state = _FinalizationState()
        cancellation: asyncio.CancelledError | None = None
        log_secrets_token = bind_log_secrets(secrets)
        try:
            logger.info(
                "Scenario started",
                extra={
                    "correlation_id": correlation_id,
                    "scenario": scenario.scenario,
                    "status": "running",
                    "tags": list(scenario.tags),
                },
            )
        except BaseException:
            secrets.clear()
            reset_log_secrets(log_secrets_token)
            raise

        try:
            async with asyncio.timeout(self.settings.scenario_timeout_seconds):
                async with self._admission.acquire(ResourceKind.SCENARIO):
                    self._emit_scenario_progress(context, result, "running")
                    await self._execute_scenario(context, result)
            result.status = "passed"
        except asyncio.CancelledError as exc:
            result.status = "cancelled"
            cancellation = exc
        except Exception as exc:  # noqa: BLE001 - scenario boundary records every failure.
            failure_state.record(
                error=exc,
                message=secrets.redact_text(str(exc)),
                result=result,
                activity=context.current_activity or "scenario",
                step_id=context.current_step_id or f"step_{len(result.steps) + 1}",
            )
        finally:
            try:
                await self._finalize(
                    started,
                    context,
                    result,
                    failure_state,
                    finalization_state,
                    publish=cancellation is None,
                )
            except BaseException:
                secrets.clear()
                reset_log_secrets(log_secrets_token)
                raise

        self._emit_scenario_progress(
            context,
            result,
            _terminal_progress_status(
                failure_state,
                finalization_state,
                cancellation,
            ),
        )
        try:
            return self._finish_run(
                context,
                result,
                failure_state,
                finalization_state,
                cancellation,
            )
        finally:
            secrets.clear()
            reset_log_secrets(log_secrets_token)

    async def _execute_scenario(
        self,
        context: RunContext,
        result: ScenarioResult,
    ) -> None:
        scenario = context.scenario
        self.validate_reporting(scenario)
        await self.registry.prepare_many(
            (step.activity for step in scenario.steps),
            context,
        )
        for position, step in enumerate(scenario.steps, start=1):
            await self._run_step(context, step.activity, step.params, position, result)
        resolved_outputs = ExpressionResolver(
            context.values,
            environment_observer=context.secrets.observe_environment,
        ).resolve(scenario.outputs)
        result.outputs = context.secrets.redact(resolved_outputs)

    def _finish_run(
        self,
        context: RunContext,
        result: ScenarioResult,
        failure_state: _FailureState,
        finalization_state: _FinalizationState,
        cancellation: asyncio.CancelledError | None,
    ) -> ScenarioResult:
        if cancellation is not None:
            self._raise_cancellation(context, result, cancellation)
        logger.info(
            "Scenario finished",
            extra={
                "correlation_id": context.correlation_id,
                "scenario": context.scenario.scenario,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "tags": list(context.scenario.tags),
            },
        )
        if failure_state.error is not None:
            exception_type = type(failure_state.error).__name__
            safe_message = failure_state.message or exception_type
            raise ActivityExecutionError(
                f"Scenario '{context.scenario.scenario}' failed: {exception_type}: {safe_message}"
            ) from None
        if finalization_state.error is not None:
            exception_type = type(finalization_state.error).__name__
            safe_message = finalization_state.message or exception_type
            raise ActivityExecutionError(
                f"Scenario '{context.scenario.scenario}' finalization failed: "
                f"{exception_type}: {safe_message}"
            ) from None
        return result

    @staticmethod
    def _raise_cancellation(
        context: RunContext,
        result: ScenarioResult,
        cancellation: asyncio.CancelledError,
    ) -> Never:
        logger.info(
            "Scenario cancelled",
            extra={
                "correlation_id": context.correlation_id,
                "scenario": context.scenario.scenario,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "tags": list(context.scenario.tags),
            },
        )
        raise cancellation

    async def _finalize(
        self,
        started: float,
        context: RunContext,
        result: ScenarioResult,
        failure_state: _FailureState,
        finalization_state: _FinalizationState,
        *,
        publish: bool,
    ) -> None:
        reporter_cleanup_attempted = False
        try:
            try:
                await self._close_services(
                    context,
                    failed=failure_state.failed or not publish,
                )
            except Exception as exc:  # noqa: BLE001 - cleanup is a scenario boundary.
                if publish:
                    failure_state.record(
                        error=exc,
                        message=context.secrets.redact_text(str(exc)),
                        result=result,
                        activity="cleanup",
                        step_id=context.current_step_id or "cleanup",
                    )
                else:
                    logger.error(  # noqa: TRY400 - cancellation details stay out of logs.
                        "Runtime cleanup failed after cancellation",
                        extra={"exception_type": type(exc).__name__},
                    )
            result.duration_ms = round((time.monotonic() - started) * 1000)
            if publish:
                self._sanitize_report_data(context, result, failure_state)
                await self._publish_result(context, result)
                if self._reporting.owns_reporter and not self._batch_active:
                    reporter_cleanup_attempted = True
                    await self._close_owned_reporter(result)
                await self._persist_allure_result(context, result)
                await self._persist_report(context, result, finalization_state)
                self._emit_terminal_event(context, result, finalization_state)
        finally:
            try:
                if (
                    self._reporting.owns_reporter
                    and not self._batch_active
                    and not reporter_cleanup_attempted
                ):
                    await self._close_owned_reporter()
            finally:
                context.values.clear()

    async def _close_services(self, context: RunContext, *, failed: bool) -> None:
        try:
            async with asyncio.timeout(self._cleanup_timeout_seconds()):
                await context.services.close(failed=failed)
        except TimeoutError as exc:
            raise ActivityExecutionError(RUNTIME_CLEANUP_TIMEOUT) from exc

    def _cleanup_timeout_seconds(self) -> float:
        return float(getattr(self.settings, "cleanup_timeout_seconds", 15.0))

    async def _close_owned_reporter(self, result: ScenarioResult | None = None) -> None:
        failure_stage: str | None = None
        try:
            async with asyncio.timeout(self._cleanup_timeout_seconds()):
                closed = await self._reporting.close()
            if not closed:
                failure_stage = "cleanup"
        except TimeoutError:
            closed = False
            failure_stage = "timeout"
            logger.error(  # noqa: TRY400 - timeout traceback adds no safe value.
                REPORTER_CLEANUP_TIMEOUT
            )
        if result is None:
            return
        integration = result.integrations.get(self._reporting.reporter.provider)
        if integration is None:
            return
        integration["cleanup_status"] = "closed" if closed else "failed"
        integration["cleanup_failure_stage"] = failure_stage

    def _emit_terminal_event(
        self,
        context: RunContext,
        result: ScenarioResult,
        finalization_state: _FinalizationState,
    ) -> None:
        try:
            emit_scenario_completed(
                result,
                environment=self.settings.environment,
                secrets=context.secrets,
            )
        except Exception as exc:  # noqa: BLE001 - telemetry failure is infrastructure failure.
            logger.error(  # noqa: TRY400 - OS details must never reach logs.
                TELEMETRY_PERSISTENCE_FAILURE,
                extra={
                    "correlation_id": context.correlation_id,
                    "scenario": context.scenario.scenario,
                    "exception_type": type(exc).__name__,
                    "failure_stage": "telemetry_persistence",
                },
            )
            finalization_state.record(
                error=exc,
                message=TELEMETRY_PERSISTENCE_FAILURE,
            )

    async def _persist_allure_result(
        self,
        context: RunContext,
        result: ScenarioResult,
    ) -> None:
        if not getattr(self.settings, "allure_results_enabled", False):
            return
        result.integrations["allure"] = {
            "provider": "allure",
            "status": "written",
            "result_file": None,
            "failure_stage": None,
        }
        try:
            report = cast(
                "dict[str, Any]",
                context.secrets.redact(result.sanitized_dict()),
            )
            target = await self._admission.run_blocking(
                write_allure_result,
                self.settings.output_dir,
                report,
                environment=self.settings.environment,
                secrets=context.secrets,
            )
            result_file = target.relative_to(self.settings.output_dir).as_posix()
        except Exception as exc:  # noqa: BLE001 - optional integration boundary.
            result.integrations["allure"] = self._allure_failure()
            logger.error(  # noqa: TRY400 - tracebacks may expose integration details.
                ALLURE_PERSISTENCE_FAILURE,
                extra={"exception_type": type(exc).__name__},
            )
            return
        result.integrations["allure"]["result_file"] = result_file
        logger.info("Allure result written", extra={"result_file": result_file})

    @staticmethod
    def _allure_failure() -> dict[str, str | int | None]:
        return {
            "provider": "allure",
            "status": "failed",
            "result_file": None,
            "failure_stage": "allure_persistence",
        }

    async def _publish_result(self, context: RunContext, result: ScenarioResult) -> None:
        report = cast(
            "dict[str, Any]",
            context.secrets.redact(result.sanitized_dict()),
        )
        publication = await self._reporting.publish(
            context.scenario,
            status=result.status,
            duration_ms=result.duration_ms,
            report=report,
            secrets=context.secrets,
        )
        result.integrations[publication.provider] = publication.as_dict()

    @staticmethod
    def _sanitize_report_data(
        context: RunContext,
        result: ScenarioResult,
        failure_state: _FailureState,
    ) -> None:
        try:
            result.artifacts = cast(
                "list[dict[str, Any]]",
                context.secrets.redact(list(context.artifacts)),
            )
            result.operations = cast(
                "list[dict[str, Any]]",
                context.secrets.redact(list(context.operations)),
            )
        except Exception as exc:  # noqa: BLE001 - sanitization must fail closed.
            result.artifacts = []
            result.operations = []
            safe_message = "Scenario report-data sanitization failed"
            logger.error(  # noqa: TRY400 - a traceback could expose unsanitized values.
                safe_message,
                extra={"exception_type": type(exc).__name__},
            )
            failure_state.record(
                error=exc,
                message=safe_message,
                result=result,
                activity="reporting",
                step_id=context.current_step_id or "reporting",
            )

    async def _persist_report(
        self,
        context: RunContext,
        result: ScenarioResult,
        finalization_state: _FinalizationState,
    ) -> None:
        try:
            report = cast(
                "dict[str, Any]",
                context.secrets.redact(result.sanitized_dict()),
            )
            await self._admission.run_blocking(self._write_report, result, report)
        except Exception as exc:  # noqa: BLE001 - preserve the primary failure.
            safe_message = REPORT_PERSISTENCE_FAILURE
            logger.error(  # noqa: TRY400 - a traceback could expose filesystem details.
                safe_message,
                extra={"exception_type": type(exc).__name__},
            )
            finalization_state.record(
                error=exc,
                message=safe_message,
            )

    @staticmethod
    def _emit_scenario_progress(
        context: RunContext,
        result: ScenarioResult,
        status: RunProgressStatus,
    ) -> None:
        context.emit_progress(
            RunProgressEvent(
                correlation_id=context.correlation_id,
                kind="scenario",
                status=status,
                completed_steps=len(result.steps),
                total_steps=len(context.scenario.steps),
                duration_ms=result.duration_ms if status != "running" else None,
            )
        )

    @staticmethod
    def _emit_step_progress(
        context: RunContext,
        result: ScenarioResult,
        position: int,
        status: RunProgressStatus,
        duration_ms: int | None = None,
    ) -> None:
        context.emit_progress(
            RunProgressEvent(
                correlation_id=context.correlation_id,
                kind="step",
                status=status,
                completed_steps=len(result.steps),
                total_steps=len(context.scenario.steps),
                activity=context.secrets.redact_text(context.current_activity or "scenario"),
                step_id=context.secrets.redact_text(context.current_step_id or "scenario"),
                position=position,
                duration_ms=duration_ms,
            )
        )

    async def _run_step(
        self,
        context: RunContext,
        activity: str,
        raw_params: dict[str, Any],
        position: int,
        scenario_result: ScenarioResult,
    ) -> None:
        step_id_value = raw_params.get("id")
        if not isinstance(step_id_value, str) or not step_id_value.strip():
            raise ActivityExecutionError(
                f"Activity '{activity}' at position {position} requires a literal non-empty id"
            )
        step_id = step_id_value.strip()
        if context.values.contains(step_id):
            raise ActivityExecutionError(f"Duplicate step id at runtime: {step_id}")

        context.current_activity = activity
        context.current_step_id = step_id
        resolver = ExpressionResolver(
            context.values,
            environment_observer=context.secrets.observe_environment,
        )
        resolved = resolver.resolve(raw_params)
        started = time.monotonic()
        logger.info(
            "Activity started",
            extra={
                "correlation_id": context.correlation_id,
                "scenario": context.scenario.scenario,
                "activity": activity,
                "step_id": step_id,
            },
        )
        self._emit_step_progress(context, scenario_result, position, "running")
        try:
            async with asyncio.timeout(self.settings.step_timeout_seconds):
                activity_result = await self.registry.execute(activity, context, resolved)
            stored = self._normalize_result(activity_result)
            context.secrets.observe(stored)
            context.values.set_result(step_id, stored)
            duration_ms = round((time.monotonic() - started) * 1000)
            scenario_result.steps.append(
                StepResult(
                    activity=activity,
                    step_id=step_id,
                    status="passed",
                    duration_ms=duration_ms,
                )
            )
            self._emit_step_progress(
                context,
                scenario_result,
                position,
                "passed",
                duration_ms,
            )
            logger.info(
                "Activity passed",
                extra={
                    "correlation_id": context.correlation_id,
                    "scenario": context.scenario.scenario,
                    "activity": activity,
                    "step_id": step_id,
                    "duration_ms": duration_ms,
                    "status": "passed",
                },
            )
        except Exception as exc:
            duration_ms = round((time.monotonic() - started) * 1000)
            safe_message = context.secrets.redact_text(str(exc))
            safe_details = self._safe_failure_details(exc, context)
            scenario_result.steps.append(
                StepResult(
                    activity=activity,
                    step_id=step_id,
                    status="failed",
                    duration_ms=duration_ms,
                )
            )
            scenario_result.failure = StepFailure(
                activity=activity,
                step_id=step_id,
                message=safe_message,
                exception_type=type(exc).__name__,
                details=safe_details or None,
            )
            logger.error(  # noqa: TRY400 - traceback could expose unsanitized secrets.
                "Activity failed: %s",
                safe_message,
                extra={
                    "correlation_id": context.correlation_id,
                    "scenario": context.scenario.scenario,
                    "activity": activity,
                    "step_id": step_id,
                    "duration_ms": duration_ms,
                    "status": "failed",
                    "exception_type": type(exc).__name__,
                    "safe_exception": safe_message,
                    **safe_details,
                },
            )
            self._emit_step_progress(
                context,
                scenario_result,
                position,
                "failed",
                duration_ms,
            )
            raise

    @staticmethod
    def _safe_failure_details(exc: Exception, context: RunContext) -> dict[str, str | int]:
        raw = getattr(exc, "safe_details", None)
        if not isinstance(raw, dict):
            return {}
        allowed = {
            "diagnostic_snapshot",
            "diagnostic_status",
            "evidence_state",
            "failure_stage",
            "http_status",
            "schema_path",
            "schema_rule",
            "operation_index",
            "operation_total",
            "operation_type",
            "operation_target",
            "operation_error_type",
        }
        result: dict[str, str | int] = {}
        for key in allowed:
            value = raw.get(key)
            if type(value) is int:
                result[key] = value
            elif isinstance(value, str):
                result[key] = context.secrets.redact_text(value[:500])
        return result

    @staticmethod
    def _normalize_result(value: Any) -> Any:
        if value is None:
            return {"success": True}
        if isinstance(value, BaseModel):
            return value.model_dump(mode="python", by_alias=True)
        return value

    def _write_report(self, result: ScenarioResult, report: dict[str, Any]) -> None:
        target = scenario_report_target(
            self.settings.output_dir,
            scenario=result.scenario,
            source_path=result.source_path,
            run_id=result.correlation_id,
        )
        write_json_atomic(target, report)

    async def run_many(
        self, scenarios: list[ScenarioDefinition], *, concurrency: int = 1
    ) -> list[ScenarioResult | BaseException]:
        if self._batch_active:
            raise RuntimeError("ScenarioRunner.run_many is not reentrant")
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        scenario_limit = self._admission.limit(ResourceKind.SCENARIO)
        if concurrency > scenario_limit:
            raise ValueError(
                f"concurrency must not exceed PLANTAIN_MAX_SCENARIO_CONCURRENCY ({scenario_limit})"
            )
        if len(scenarios) > MAX_DISCOVERED_SCENARIO_FILES:
            raise ValueError(
                f"scenario batch must not exceed {MAX_DISCOVERED_SCENARIO_FILES} entries"
            )
        if not scenarios:
            return []
        shared_pool_factory: Callable[[], DatabasePoolManager] | None
        if self._owns_database_pools:
            shared_pools = None
            shared_pool_factory = self._active_database_pools
        else:
            shared_pools = self._database_pools
            shared_pool_factory = self._database_pool_factory
        self._batch_active = True
        positions = iter(range(len(scenarios)))
        results: list[ScenarioResult | BaseException | None] = [None] * len(scenarios)

        async def worker() -> None:
            while (position := next(positions, None)) is not None:
                runner = ScenarioRunner(
                    self.settings,
                    self.registry,
                    database_pools=shared_pools,
                    database_pool_factory=shared_pool_factory,
                    result_reporter=self._reporting.reporter,
                    admission=self._admission,
                    progress_observer=self._progress_observer,
                )
                try:
                    results[position] = await runner.run(scenarios[position])
                except Exception as exc:  # noqa: BLE001 - scenario failures are result values.
                    results[position] = exc

        worker_count = min(concurrency, len(scenarios))
        try:
            await asyncio.gather(*(worker() for _ in range(worker_count)))
        finally:
            self._batch_active = False
            try:
                pools = self._database_pools
                if self._owns_database_pools and pools is not None and not pools.closed:
                    await self._admission.run_blocking(pools.close)
            finally:
                if self._reporting.owns_reporter:
                    await self._reporting.close()
        if any(result is None for result in results):
            raise ActivityExecutionError("Concurrent scenario execution produced no result")
        # Indexed worker writes preserve the caller's deterministic input order.
        return cast("list[ScenarioResult | BaseException]", results)
