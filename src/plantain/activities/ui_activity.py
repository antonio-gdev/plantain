"""Single YAML-facing UI facade backed by focused generic services."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, TypeVar
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Page, Response

from plantain.activities.snapshot_bundle import (
    DEFAULT_MAX_CAPTURE_BYTES,
    DEFAULT_RECORD_CHUNK_BYTES,
)
from plantain.activities.snapshot_registry_complete import (
    CompleteSnapshotRegistry,
    DiagnosticStage,
    EvidenceState,
    PendingSnapshot,
    RegistrationResult,
    SnapshotRegistrationCancellation,
    SnapshotRegistrationCancellationError,
)
from plantain.activities.ui_actions import ActionOutcome, UiActionExecutor
from plantain.activities.ui_assertions import UiAssertionEngine
from plantain.activities.ui_browser import BrowserSession
from plantain.activities.ui_errors import (
    BrowserLifecycleError,
    SnapshotConsistencyError,
    SnapshotError,
    UiActionError,
    UiAssertionError,
)
from plantain.activities.ui_locator import LocatorResolver
from plantain.activities.ui_snapshot_complete import CompleteSemanticSnapshotExtractor
from plantain.engine.registry import ActivityRegistry
from plantain.engine.runtime import RunContext
from plantain.models.ui import (
    CapturePageSnapshotParams,
    CapturePageSnapshotResult,
    LocatorSpec,
    SnapshotArtifact,
    UiAction,
    UiAssertion,
)
from plantain.observability import get_logger

logger = get_logger("activities.ui")
MAX_OPERATION_TARGET_LENGTH = 200
MAX_OPERATION_VALUE_LENGTH = 500
MAX_SNAPSHOT_CAPTURE_ATTEMPTS = 3
_T = TypeVar("_T")


@dataclass(slots=True)
class _UiLifecycle:
    browser: BrowserSession
    page: Page
    actions: UiActionExecutor
    assertions: UiAssertionEngine
    extractor: CompleteSemanticSnapshotExtractor
    activity_name: str
    timeout_ms: int


@dataclass(slots=True)
class _ActionPhase:
    pending: list[PendingSnapshot] = field(default_factory=list)
    failure: UiActionError | None = None
    snapshot_position: int = 0


@dataclass(slots=True)
class _RegistrationPhase:
    artifacts: list[SnapshotArtifact]
    registry: CompleteSnapshotRegistry | None = None


@dataclass(slots=True)
class _OperationTrace:
    operation_type: str
    target: str
    extra: dict[str, Any]
    started: float


@dataclass(slots=True)
class _UiStepTrace:
    operation_type: str
    target: str
    suffix: str
    position: int
    total: int
    extra: dict[str, Any]
    started: float


async def _run_action_phase(
    context: RunContext,
    params: CapturePageSnapshotParams,
    lifecycle: _UiLifecycle,
) -> _ActionPhase:
    phase = _ActionPhase()
    try:
        total = len(params.actions)
        for position, action in enumerate(params.actions, start=1):
            try:
                await _execute_action(
                    context,
                    lifecycle,
                    action,
                    position=position,
                    total=total,
                )
            except UiActionError as exc:
                phase.failure = exc
                break
            await _stage_intermediate_snapshot(
                context,
                params,
                lifecycle,
                phase,
                action_position=position,
            )
        await _stage_final_snapshot(context, params, lifecycle, phase)
        if phase.failure is None:
            await lifecycle.browser.assert_policy_compliant()
    except (Exception, asyncio.CancelledError) as exc:
        await _cleanup_failed_action_phase(context, phase, exc)
        raise
    return phase


async def _cleanup_failed_action_phase(
    context: RunContext,
    phase: _ActionPhase,
    primary: BaseException,
) -> None:
    try:
        await _await_snapshot_work(
            context.services.admission.run_blocking(_cleanup_pending, phase.pending)
        )
    except Exception as cleanup_exc:  # noqa: BLE001 - cleanup failure must remain visible.
        raise SnapshotError(
            "UI capture failed and private staging cleanup failed"
        ) from BaseExceptionGroup(
            "UI capture and cleanup failures",
            [primary, cleanup_exc],
        )


async def _stage_final_snapshot(
    context: RunContext,
    params: CapturePageSnapshotParams,
    lifecycle: _UiLifecycle,
    phase: _ActionPhase,
) -> None:
    if not params.snapshot.enabled:
        return
    phase.snapshot_position += 1
    evidence_state, failure_stage = _snapshot_evidence(
        action_failed=phase.failure is not None,
        verification_expected=bool(params.verify),
    )
    try:
        staged = await _capture_snapshot(
            context,
            lifecycle.browser,
            lifecycle.extractor,
            lifecycle.page,
            activity=lifecycle.activity_name,
            position=phase.snapshot_position,
            total=_snapshot_total(params),
            diagnostic_after_action_failure=phase.failure is not None,
        )
    except Exception as exc:
        if phase.failure is None:
            raise
        phase.failure.safe_details["diagnostic_status"] = "capture_failed"
        raise phase.failure from exc
    phase.pending.append(
        PendingSnapshot(
            staged=staged,
            activity=lifecycle.activity_name,
            evidence_state=evidence_state,
            failure_stage=failure_stage,
        )
    )


async def _stage_intermediate_snapshot(
    context: RunContext,
    params: CapturePageSnapshotParams,
    lifecycle: _UiLifecycle,
    phase: _ActionPhase,
    *,
    action_position: int,
) -> None:
    if not (
        params.snapshot.enabled
        and params.snapshot.capture_after_each_action
        and action_position < len(params.actions)
    ):
        return
    activity = f"{lifecycle.activity_name}__after_{action_position}"
    phase.snapshot_position += 1
    staged = await _capture_snapshot(
        context,
        lifecycle.browser,
        lifecycle.extractor,
        lifecycle.page,
        activity=activity,
        position=phase.snapshot_position,
        total=_snapshot_total(params),
    )
    phase.pending.append(PendingSnapshot(staged=staged, activity=activity))


async def _execute_action(
    context: RunContext,
    lifecycle: _UiLifecycle,
    action: UiAction,
    *,
    position: int,
    total: int,
) -> None:
    trace = _action_trace(context, action, position=position, total=total)
    _record_action_started(trace)
    try:
        await lifecycle.browser.assert_policy_compliant()
        outcome, operation_actual = await _perform_action(
            context,
            lifecycle.browser,
            lifecycle.actions,
            lifecycle.page,
            action,
            timeout_ms=lifecycle.timeout_ms,
        )
        lifecycle.page = outcome.page
        if outcome.popup_opened:
            lifecycle.browser.activate(lifecycle.page)
        await lifecycle.browser.assert_policy_compliant()
        if operation_actual is not None:
            trace.extra["operation_actual"] = operation_actual
    except Exception as exc:
        raise _action_error(context, trace, exc) from exc
    _record_action_success(context, trace)


def _record_action_success(context: RunContext, trace: _UiStepTrace) -> None:
    duration_ms = _duration_ms(trace.started)
    _record_ui_operation(
        context,
        trace.extra,
        status="passed",
        duration_ms=duration_ms,
    )
    logger.info(
        "UI action %d/%d passed: %s target=%s%s",
        trace.position,
        trace.total,
        trace.operation_type,
        trace.target,
        trace.suffix,
        extra={
            **trace.extra,
            "duration_ms": duration_ms,
            "status": "passed",
        },
    )


def _action_error(
    context: RunContext,
    trace: _UiStepTrace,
    exc: Exception,
) -> UiActionError:
    details = _action_failure_details(trace, exc)
    _record_action_failed(context, trace, exc, details)
    return UiActionError(
        f"UI action {trace.position}/{trace.total} failed: "
        f"{trace.operation_type} target={trace.target}",
        safe_details=details,
    )


def _record_action_failed(
    context: RunContext,
    trace: _UiStepTrace,
    exc: Exception,
    details: dict[str, str | int],
) -> None:
    duration_ms = _duration_ms(trace.started)
    logger.error(
        "UI action %d/%d failed: %s target=%s%s",
        trace.position,
        trace.total,
        trace.operation_type,
        trace.target,
        trace.suffix,
        extra={
            **trace.extra,
            "duration_ms": duration_ms,
            "status": "failed",
            "exception_type": type(exc).__name__,
            **details,
        },
    )
    _record_ui_operation(
        context,
        trace.extra,
        status="failed",
        duration_ms=duration_ms,
        error_type=_operation_error_type(exc),
    )


def _action_failure_details(
    trace: _UiStepTrace,
    exc: Exception,
) -> dict[str, str | int]:
    return {
        "failure_stage": "ui_action",
        "operation_index": trace.position,
        "operation_total": trace.total,
        "operation_type": trace.operation_type,
        "operation_target": trace.target,
        "operation_error_type": _operation_error_type(exc),
    }


def _record_action_started(trace: _UiStepTrace) -> None:
    logger.info(
        "UI action %d/%d started: %s target=%s%s",
        trace.position,
        trace.total,
        trace.operation_type,
        trace.target,
        trace.suffix,
        extra=trace.extra,
    )


def _action_trace(
    context: RunContext,
    action: UiAction,
    *,
    position: int,
    total: int,
) -> _UiStepTrace:
    action_type = action.action.value
    target = _locator_target(
        context,
        action.target,
        fallback="page.url" if action_type == "waitForUrl" else "page",
    )
    operation_input = _action_input(context, action, target)
    extra = _ui_extra(
        context,
        phase="action",
        operation_type=action_type,
        target=target,
        position=position,
        total=total,
        operation_input=operation_input,
        operation_expected=_action_expected(context, action, target),
    )
    return _UiStepTrace(
        action_type,
        target,
        _operation_suffix("input", operation_input),
        position,
        total,
        extra,
        time.monotonic(),
    )


def _navigation_failure_details(
    trace: _OperationTrace,
    exc: Exception,
) -> dict[str, str | int]:
    return {
        "failure_stage": "ui_navigation",
        "operation_type": trace.operation_type,
        "operation_target": trace.target,
        "operation_error_type": _operation_error_type(exc),
    }


def _raise_navigation_failure(
    context: RunContext,
    trace: _OperationTrace,
    exc: Exception,
) -> NoReturn:
    duration_ms = _duration_ms(trace.started)
    details = _navigation_failure_details(trace, exc)
    error_type = str(details["operation_error_type"])
    logger.error(
        "UI navigation failed: %s target=%s",
        trace.operation_type,
        trace.target,
        extra={
            **trace.extra,
            "duration_ms": duration_ms,
            "status": "failed",
            "exception_type": type(exc).__name__,
            **details,
        },
    )
    _record_ui_operation(
        context,
        trace.extra,
        status="failed",
        duration_ms=duration_ms,
        error_type=error_type,
    )
    raise BrowserLifecycleError("UI navigation failed", safe_details=details) from exc


def _record_navigation_success(context: RunContext, trace: _OperationTrace) -> None:
    duration_ms = _duration_ms(trace.started)
    _record_ui_operation(
        context,
        trace.extra,
        status="passed",
        duration_ms=duration_ms,
    )
    logger.info(
        "UI navigation passed: %s target=%s",
        trace.operation_type,
        trace.target,
        extra={
            **trace.extra,
            "duration_ms": duration_ms,
            "status": "passed",
        },
    )


def _navigation_trace(
    context: RunContext,
    params: CapturePageSnapshotParams,
) -> _OperationTrace:
    operation_type = "navigate" if params.url is not None else "resume"
    target = _navigation_target(context, params.url) if params.url is not None else "page"
    extra = _ui_extra(
        context,
        phase="navigation",
        operation_type=operation_type,
        target=target,
    )
    logger.info(
        "UI navigation started: %s target=%s",
        operation_type,
        target,
        extra=extra,
    )
    return _OperationTrace(operation_type, target, extra, time.monotonic())


async def _navigate_page(
    context: RunContext,
    browser: BrowserSession,
    params: CapturePageSnapshotParams,
) -> Page:
    trace = _navigation_trace(context, params)
    try:
        page = (
            await browser.navigate(params.url, wait_until=params.wait_until)
            if params.url is not None
            else await browser.start()
        )
        await browser.assert_policy_compliant()
    except Exception as exc:  # noqa: BLE001 - normalize to a value-free lifecycle error.
        _raise_navigation_failure(context, trace, exc)
    _record_navigation_success(context, trace)
    return page


async def _run_registration_phase(
    context: RunContext,
    lifecycle: _UiLifecycle,
    action_phase: _ActionPhase,
) -> _RegistrationPhase:
    if not action_phase.pending:
        return _RegistrationPhase([])
    registry = await _acquire_snapshot_registry(context, action_phase)
    trace = _registration_trace(context, len(action_phase.pending))
    try:
        registrations = await _register_pending_snapshots(
            context,
            registry,
            action_phase.pending,
        )
    except Exception as exc:  # noqa: BLE001 - translate backend failures safely.
        _raise_registration_failure(context, trace, action_phase, exc)
    commit_telemetry = getattr(lifecycle.extractor, "commit_telemetry", None)
    if callable(commit_telemetry):
        commit_telemetry()
    _record_registration_success(context, trace, registrations)
    return _RegistrationPhase(
        artifacts=_snapshot_artifacts(context, registrations),
        registry=registry,
    )


def _snapshot_artifacts(
    context: RunContext,
    registrations: Sequence[RegistrationResult],
) -> list[SnapshotArtifact]:
    artifacts: list[SnapshotArtifact] = []
    for registration in registrations:
        relative = Path("snapshots") / registration.canonical_file
        is_diagnostic = registration.evidence_state == "diagnostic"
        context.add_artifact(
            kind=("semantic-snapshot-diagnostic" if is_diagnostic else "semantic-snapshot"),
            path=relative.as_posix(),
            description=(
                f"Complete diagnostic DOM evidence for {registration.activity}"
                if is_diagnostic
                else f"Canonical DOM state for {registration.activity}"
            ),
        )
        artifacts.append(
            SnapshotArtifact(
                activity=registration.activity,
                canonical_file=relative.as_posix(),
                status=registration.status,
                evidence_state=registration.evidence_state,
                failure_stage=registration.failure_stage,
            )
        )
    return artifacts


def _record_registration_success(
    context: RunContext,
    trace: _OperationTrace,
    registrations: Sequence[RegistrationResult],
) -> None:
    total = len(registrations)
    duration_ms = _duration_ms(trace.started)
    _record_ui_operation(
        context,
        {**trace.extra, "operation_input": f"artifacts={total}"},
        status="passed",
        duration_ms=duration_ms,
    )
    logger.info(
        "UI snapshot registration passed: register target=snapshot-registry (%d artifact(s))",
        total,
        extra={
            **trace.extra,
            "duration_ms": duration_ms,
            "status": "passed",
        },
    )


def _raise_registration_failure(
    context: RunContext,
    trace: _OperationTrace,
    phase: _ActionPhase,
    exc: Exception,
) -> NoReturn:
    total = len(phase.pending)
    details = _registration_failure_details(trace, total, exc)
    _record_registration_failed(context, trace, total, exc, details)
    if phase.failure is not None:
        phase.failure.safe_details["diagnostic_status"] = "registration_failed"
        raise phase.failure from exc
    raise SnapshotError(
        "UI snapshot registration failed",
        safe_details=details,
    ) from exc


def _record_registration_failed(
    context: RunContext,
    trace: _OperationTrace,
    total: int,
    exc: Exception,
    details: dict[str, str | int],
) -> None:
    duration_ms = _duration_ms(trace.started)
    logger.error(
        "UI snapshot registration failed: register target=snapshot-registry (%d artifact(s))",
        total,
        extra={
            **trace.extra,
            "duration_ms": duration_ms,
            "status": "failed",
            "exception_type": type(exc).__name__,
            **details,
        },
    )
    _record_ui_operation(
        context,
        {**trace.extra, "operation_input": f"artifacts={total}"},
        status="failed",
        duration_ms=duration_ms,
        error_type=_operation_error_type(exc),
    )


def _registration_failure_details(
    trace: _OperationTrace,
    total: int,
    exc: Exception,
) -> dict[str, str | int]:
    return {
        "failure_stage": "snapshot_registration",
        "operation_total": total,
        "operation_type": trace.operation_type,
        "operation_target": trace.target,
        "operation_error_type": _operation_error_type(exc),
    }


def _raise_deferred_action_failure(
    failure: UiActionError,
    artifacts: Sequence[SnapshotArtifact],
) -> NoReturn:
    diagnostic = next(
        (item for item in artifacts if item.failure_stage == "action"),
        None,
    )
    if diagnostic is not None:
        failure.safe_details.update(
            diagnostic_snapshot=diagnostic.canonical_file,
            evidence_state=diagnostic.evidence_state,
        )
    raise failure from failure.__cause__


async def _run_verification_phase(
    context: RunContext,
    params: CapturePageSnapshotParams,
    lifecycle: _UiLifecycle,
    artifacts: Sequence[SnapshotArtifact],
) -> None:
    total = len(params.verify)
    for position, assertion in enumerate(params.verify, start=1):
        await _execute_verification(
            context,
            lifecycle,
            assertion,
            artifacts,
            position=position,
            total=total,
        )


async def _execute_verification(
    context: RunContext,
    lifecycle: _UiLifecycle,
    assertion: UiAssertion,
    artifacts: Sequence[SnapshotArtifact],
    *,
    position: int,
    total: int,
) -> None:
    trace = _verification_trace(context, assertion, position=position, total=total)
    _record_verification_started(trace)
    try:
        await lifecycle.browser.assert_policy_compliant()
        await lifecycle.assertions.verify(lifecycle.page, assertion)
        await lifecycle.browser.assert_policy_compliant()
    except Exception as exc:
        raise _verification_error(context, trace, artifacts, exc) from exc
    _record_verification_success(context, trace)


def _verification_trace(
    context: RunContext,
    assertion: UiAssertion,
    *,
    position: int,
    total: int,
) -> _UiStepTrace:
    assertion_type = assertion.assertion.value
    fallback = (
        "page.url"
        if assertion_type == "url"
        else "page.title"
        if assertion_type == "title"
        else "page"
    )
    target = _locator_target(context, assertion.target, fallback=fallback)
    expected = _verification_expected(context, assertion, target)
    extra = _ui_extra(
        context,
        phase="verification",
        operation_type=assertion_type,
        target=target,
        position=position,
        total=total,
        operation_expected=expected,
    )
    return _UiStepTrace(
        assertion_type,
        target,
        _operation_suffix("expected", expected),
        position,
        total,
        extra,
        time.monotonic(),
    )


def _record_verification_started(trace: _UiStepTrace) -> None:
    logger.info(
        "UI verification %d/%d started: %s target=%s%s",
        trace.position,
        trace.total,
        trace.operation_type,
        trace.target,
        trace.suffix,
        extra=trace.extra,
    )


def _verification_failure_details(
    trace: _UiStepTrace,
    exc: Exception,
) -> dict[str, str | int]:
    return {
        "failure_stage": "ui_verification",
        "operation_index": trace.position,
        "operation_total": trace.total,
        "operation_type": trace.operation_type,
        "operation_target": trace.target,
        "operation_error_type": _operation_error_type(exc),
    }


def _verification_error(
    context: RunContext,
    trace: _UiStepTrace,
    artifacts: Sequence[SnapshotArtifact],
    exc: Exception,
) -> UiAssertionError:
    details = _verification_failure_details(trace, exc)
    diagnostic = next(
        (item for item in artifacts if item.failure_stage == "verification"),
        None,
    )
    if diagnostic is not None:
        details.update(
            diagnostic_snapshot=diagnostic.canonical_file,
            evidence_state=diagnostic.evidence_state,
        )
    _record_verification_failed(context, trace, exc, details)
    return UiAssertionError(
        f"UI verification {trace.position}/{trace.total} failed: "
        f"{trace.operation_type} target={trace.target}",
        safe_details=details,
    )


def _record_verification_failed(
    context: RunContext,
    trace: _UiStepTrace,
    exc: Exception,
    details: dict[str, str | int],
) -> None:
    duration_ms = _duration_ms(trace.started)
    logger.error(
        "UI verification %d/%d failed: %s target=%s%s",
        trace.position,
        trace.total,
        trace.operation_type,
        trace.target,
        trace.suffix,
        extra={
            **trace.extra,
            "duration_ms": duration_ms,
            "status": "failed",
            "exception_type": type(exc).__name__,
            **details,
        },
    )
    _record_ui_operation(
        context,
        trace.extra,
        status="failed",
        duration_ms=duration_ms,
        error_type=_operation_error_type(exc),
    )


def _record_verification_success(
    context: RunContext,
    trace: _UiStepTrace,
) -> None:
    duration_ms = _duration_ms(trace.started)
    _record_ui_operation(
        context,
        trace.extra,
        status="passed",
        duration_ms=duration_ms,
    )
    logger.info(
        "UI verification %d/%d passed: %s target=%s%s",
        trace.position,
        trace.total,
        trace.operation_type,
        trace.target,
        trace.suffix,
        extra={
            **trace.extra,
            "duration_ms": duration_ms,
            "status": "passed",
        },
    )


def _registration_trace(context: RunContext, total: int) -> _OperationTrace:
    extra = _ui_extra(
        context,
        phase="snapshot_registration",
        operation_type="register",
        target="snapshot-registry",
        total=total,
    )
    logger.info(
        "UI snapshot registration started: register target=snapshot-registry (%d artifact(s))",
        total,
        extra=extra,
    )
    return _OperationTrace("register", "snapshot-registry", extra, time.monotonic())


async def _acquire_snapshot_registry(
    context: RunContext,
    phase: _ActionPhase,
) -> CompleteSnapshotRegistry:
    try:
        return await context.services.snapshot_registry()
    except (Exception, asyncio.CancelledError) as exc:
        try:
            await _await_snapshot_work(
                context.services.admission.run_blocking(_cleanup_pending, phase.pending)
            )
        except Exception as cleanup_exc:  # noqa: BLE001 - cleanup failure must remain visible.
            raise SnapshotError(
                "Snapshot registry acquisition failed and private staging cleanup failed"
            ) from BaseExceptionGroup(
                "snapshot registry acquisition and cleanup failures",
                [exc, cleanup_exc],
            )
        if isinstance(exc, asyncio.CancelledError):
            raise
        if phase.failure is not None:
            phase.failure.safe_details["diagnostic_status"] = "registration_unavailable"
            raise phase.failure from exc
        raise SnapshotError("UI snapshot registry is unavailable") from exc


async def _promote_verification_diagnostic(
    context: RunContext,
    phase: _RegistrationPhase,
) -> None:
    diagnostic = next(
        (item for item in phase.artifacts if item.failure_stage == "verification"),
        None,
    )
    if diagnostic is None:
        return
    if phase.registry is None:
        raise SnapshotError("Verification diagnostic registry is unavailable")
    await _promote_diagnostic_snapshot(context, phase.registry, diagnostic)
    _mark_snapshot_artifact_verified(context, phase.artifacts, diagnostic)


async def _capture_result(
    context: RunContext,
    params: CapturePageSnapshotParams,
    lifecycle: _UiLifecycle,
    artifacts: list[SnapshotArtifact],
) -> CapturePageSnapshotResult:
    await lifecycle.browser.assert_policy_compliant()
    title = await lifecycle.page.title()
    await lifecycle.browser.assert_policy_compliant()
    return CapturePageSnapshotResult(
        url=context.secrets.redact_url(lifecycle.page.url),
        page_title=str(context.secrets.redact(title)),
        actions_executed=len(params.actions),
        verifications_passed=len(params.verify),
        snapshots=artifacts,
    )


def _build_ui_lifecycle(
    context: RunContext,
    params: CapturePageSnapshotParams,
    browser: BrowserSession,
    page: Page,
) -> _UiLifecycle:
    timeout_ms = params.timeout_ms or context.settings.action_timeout_ms
    locators = LocatorResolver(timeout_ms)
    return _UiLifecycle(
        browser=browser,
        page=page,
        actions=UiActionExecutor(locators, timeout_ms),
        assertions=UiAssertionEngine(locators, timeout_ms),
        extractor=_snapshot_extractor(context, browser),
        activity_name=params.activity or context.current_step_id or "captured_page",
        timeout_ms=timeout_ms,
    )


def _snapshot_extractor(
    context: RunContext,
    browser: BrowserSession,
) -> CompleteSemanticSnapshotExtractor:
    return CompleteSemanticSnapshotExtractor(
        secrets=context.secrets,
        snapshots_dir=context.settings.snapshots_dir,
        telemetry=browser.telemetry,
        max_capture_bytes=getattr(
            context.settings,
            "snapshot_max_capture_bytes",
            DEFAULT_MAX_CAPTURE_BYTES,
        ),
        working_set_bytes=getattr(
            context.settings,
            "snapshot_working_set_bytes",
            DEFAULT_RECORD_CHUNK_BYTES,
        ),
    )


async def capture_page_snapshot(
    context: RunContext,
    params: CapturePageSnapshotParams,
) -> CapturePageSnapshotResult:
    """Navigate, interact, verify, and optionally canonicalize semantic DOM state."""

    browser = await context.services.browser()
    page = await _navigate_page(context, browser, params)
    lifecycle = _build_ui_lifecycle(context, params, browser, page)
    action_phase = await _run_action_phase(context, params, lifecycle)
    action_failure = action_phase.failure
    registration_phase = await _run_registration_phase(context, lifecycle, action_phase)
    artifacts = registration_phase.artifacts

    if action_failure is not None:
        _raise_deferred_action_failure(action_failure, artifacts)

    await _run_verification_phase(context, params, lifecycle, artifacts)
    await _promote_verification_diagnostic(context, registration_phase)
    return await _capture_result(context, params, lifecycle, artifacts)


async def _capture_snapshot(
    context: RunContext,
    browser: BrowserSession,
    extractor: CompleteSemanticSnapshotExtractor,
    page: Any,
    *,
    activity: str,
    position: int,
    total: int,
    diagnostic_after_action_failure: bool = False,
) -> Any:
    target = "page"
    safe_activity = _target_value(context, activity)
    extra = _ui_extra(
        context,
        phase="snapshot_capture",
        operation_type="capture",
        target=target,
        position=position,
        total=total,
    )
    logger.info(
        "UI snapshot %d/%d capture started: target=%s",
        position,
        total,
        target,
        extra={**extra, "snapshot_activity": safe_activity},
    )
    started = time.monotonic()
    staged: Any | None = None
    try:
        if not diagnostic_after_action_failure:
            await browser.assert_policy_compliant()
        for attempt in range(1, MAX_SNAPSHOT_CAPTURE_ATTEMPTS + 1):
            try:
                staged = await extractor.capture(page, activity=activity)
            except SnapshotConsistencyError:
                if not diagnostic_after_action_failure:
                    await browser.assert_policy_compliant()
                if attempt == MAX_SNAPSHOT_CAPTURE_ATTEMPTS:
                    raise
                continue
            break
        if not diagnostic_after_action_failure:
            await browser.assert_policy_compliant()
    except (Exception, asyncio.CancelledError) as exc:
        if staged is not None:
            try:
                await _await_snapshot_work(context.services.admission.run_blocking(staged.cleanup))
            except Exception as cleanup_exc:  # noqa: BLE001 - cleanup failure must remain visible.
                raise SnapshotError(
                    "UI snapshot capture failed and private staging cleanup failed"
                ) from BaseExceptionGroup(
                    "snapshot capture and cleanup failures",
                    [exc, cleanup_exc],
                )
        if isinstance(exc, asyncio.CancelledError):
            raise
        error_type = _operation_error_type(exc)
        duration_ms = _duration_ms(started)
        details: dict[str, str | int] = {
            "failure_stage": "snapshot_capture",
            "operation_index": position,
            "operation_total": total,
            "operation_type": "capture",
            "operation_target": target,
            "operation_error_type": error_type,
        }
        logger.error(  # noqa: TRY400 - do not emit an unsanitized exception traceback.
            "UI snapshot %d/%d capture failed: target=%s",
            position,
            total,
            target,
            extra={
                **extra,
                "snapshot_activity": safe_activity,
                "duration_ms": duration_ms,
                "status": "failed",
                "exception_type": type(exc).__name__,
                "failure_stage": "snapshot_capture",
                "operation_error_type": error_type,
            },
        )
        _record_ui_operation(
            context,
            {**extra, "operation_input": f"activity={safe_activity}"},
            status="failed",
            duration_ms=duration_ms,
            error_type=error_type,
        )
        raise SnapshotError(
            f"UI snapshot {position}/{total} capture failed: target={target}",
            safe_details=details,
        ) from exc
    duration_ms = _duration_ms(started)
    _record_ui_operation(
        context,
        {**extra, "operation_input": f"activity={safe_activity}"},
        status="passed",
        duration_ms=duration_ms,
    )
    logger.info(
        "UI snapshot %d/%d capture passed: target=%s",
        position,
        total,
        target,
        extra={
            **extra,
            "snapshot_activity": safe_activity,
            "duration_ms": duration_ms,
            "status": "passed",
        },
    )
    return staged


def _snapshot_total(params: CapturePageSnapshotParams) -> int:
    if not params.snapshot.enabled:
        return 0
    intermediate = (
        max(len(params.actions) - 1, 0) if params.snapshot.capture_after_each_action else 0
    )
    return intermediate + 1


def _locator_target(
    context: RunContext,
    locator: LocatorSpec | None,
    *,
    fallback: str,
) -> str:
    if locator is None:
        return fallback

    if locator.role is not None:
        target = f"role:{_target_value(context, locator.role)}"
        if locator.name is not None:
            target += f"[name={_target_value(context, locator.name)}]"
    else:
        strategies = (
            ("label", locator.label),
            ("placeholder", locator.placeholder),
            ("text", locator.text),
            ("altText", locator.alt_text),
            ("title", locator.title),
            ("testId", locator.test_id),
            ("css", locator.css),
            ("xpath", locator.xpath),
        )
        target = next(
            f"{strategy}:{_target_value(context, value)}"
            for strategy, value in strategies
            if value is not None
        )

    frames: list[str] = []
    for frame in locator.frames:
        if frame.name is not None:
            frames.append(f"frame.name:{_target_value(context, frame.name)}")
        elif frame.url is not None:
            frames.append(f"frame.url:{_target_value(context, frame.url)}")
        else:
            frames.append(f"frame.css:{_target_value(context, frame.css or '')}")
    if frames:
        target = " > ".join([*frames, target])
    if locator.nth is not None:
        target += f"[nth={locator.nth}]"
    return target[:MAX_OPERATION_TARGET_LENGTH]


def _navigation_target(context: RunContext, url: str) -> str:
    sanitized = context.secrets.redact_url(url)
    try:
        parts = urlsplit(sanitized)
    except ValueError:
        return _target_value(context, sanitized)
    if not parts.scheme or not parts.netloc:
        return _target_value(context, sanitized)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[
        :MAX_OPERATION_TARGET_LENGTH
    ]


def _target_value(context: RunContext, value: str) -> str:
    sanitized = (
        context.secrets.redact_url(value)
        if value.startswith(("http://", "https://"))
        else context.secrets.redact_text(value)
    )
    normalized = " ".join(sanitized.split())
    return normalized[:MAX_OPERATION_TARGET_LENGTH] or "<empty>"


async def _perform_action(
    context: RunContext,
    browser: BrowserSession,
    actions: UiActionExecutor,
    page: Page,
    action: UiAction,
    *,
    timeout_ms: int,
) -> tuple[ActionOutcome, str | None]:
    expectation = getattr(action, "expect_response", None)
    if expectation is None:
        return await actions.execute(page, action), None
    waiter = browser.expect_response(
        url_pattern=expectation.url,
        method=expectation.method.value,
        statuses=expectation.statuses,
        timeout_ms=expectation.timeout_ms or action.timeout_ms or timeout_ms,
    )
    async with waiter as response_info:
        outcome = await actions.execute(page, action)
    response: Response = await response_info.value
    actual = _operation_value(
        context,
        "response",
        {
            "url": _target_value(context, response.url),
            "method": response.request.method,
            "status": response.status,
        },
    )
    return outcome, actual


def _action_input(context: RunContext, action: Any, target: str) -> str | None:
    action_type = action.action.value
    raw: object | None = None
    if action_type in {"fill", "type", "waitForUrl"}:
        raw = getattr(action, "value", None)
    elif action_type == "press":
        raw = getattr(action, "key", None) or getattr(action, "value", None)
    elif action_type == "select":
        raw = [
            choice.model_dump(mode="python", by_alias=True, exclude_none=True)
            for choice in action.choices
        ]
    elif action_type == "waitFor":
        raw = getattr(action, "state", None)
    return None if raw is None else _operation_value(context, target, raw)


def _action_expected(context: RunContext, action: Any, target: str) -> str | None:
    expectation = getattr(action, "expect_response", None)
    if expectation is None:
        return None
    return _operation_value(
        context,
        target,
        expectation.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def _verification_expected(
    context: RunContext,
    assertion: Any,
    target: str,
) -> str | None:
    contains = getattr(assertion, "contains", None)
    equals = getattr(assertion, "equals", None)
    if contains is not None:
        raw: object = {"contains": contains}
    elif equals is not None:
        raw = {"equals": equals}
    else:
        return None
    attribute = getattr(assertion, "attribute", None)
    if attribute is not None and isinstance(raw, dict):
        raw = {"attribute": attribute, **raw}
    return _operation_value(context, target, raw)


def _operation_value(context: RunContext, target: str, value: object) -> str:
    sanitized = context.secrets.redact_log_field(target, value)
    normalized = " ".join(str(sanitized).split())
    return normalized[:MAX_OPERATION_VALUE_LENGTH] or "<empty>"


def _operation_suffix(label: str, value: str | None) -> str:
    return "" if value is None else f" {label}={value}"


def _ui_extra(
    context: RunContext,
    *,
    phase: str,
    operation_type: str,
    target: str,
    position: int | None = None,
    total: int | None = None,
    operation_input: str | None = None,
    operation_expected: str | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "correlation_id": context.correlation_id,
        "scenario": context.scenario.scenario,
        "activity": context.current_activity or "capturePageSnapshot",
        "step_id": context.current_step_id or "capturePageSnapshot",
        "ui_phase": phase,
        "operation_type": operation_type,
        "operation_target": target,
    }
    if position is not None:
        extra["operation_index"] = position
    if total is not None:
        extra["operation_total"] = total
    if operation_input is not None:
        extra["operation_input"] = operation_input
    if operation_expected is not None:
        extra["operation_expected"] = operation_expected
    return extra


def _record_ui_operation(
    context: RunContext,
    extra: dict[str, object],
    *,
    status: str,
    duration_ms: int,
    error_type: str | None = None,
) -> None:
    position = extra.get("operation_index")
    total = extra.get("operation_total")
    context.add_operation(
        domain="ui",
        phase=str(extra["ui_phase"]),
        operation_type=str(extra["operation_type"]),
        target=str(extra["operation_target"]),
        status=status,
        duration_ms=duration_ms,
        position=position if type(position) is int else None,
        total=total if type(total) is int else None,
        operation_input=extra.get("operation_input"),
        operation_expected=extra.get("operation_expected"),
        operation_actual=extra.get("operation_actual"),
        error_type=error_type,
    )


async def _await_snapshot_work(
    operation: Coroutine[Any, Any, _T],
    *,
    cancel: Callable[[], None] | None = None,
    expected_error: type[Exception] | None = None,
) -> _T:
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if cancel is not None:
            cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if cancel is not None:
                    cancel()
            except Exception:  # noqa: BLE001 - inspect the completed task below.
                break
        try:
            task.result()
        except Exception as exc:
            if expected_error is None or not isinstance(exc, expected_error):
                raise SnapshotError("Snapshot cancellation cleanup failed") from exc
        raise


async def _register_pending_snapshots(
    context: RunContext,
    registry: CompleteSnapshotRegistry,
    pending: list[PendingSnapshot],
) -> list[RegistrationResult]:
    cancellation = SnapshotRegistrationCancellation()
    return await _await_snapshot_work(
        context.services.admission.run_blocking(
            registry.register_batch,
            pending,
            cancellation=cancellation,
        ),
        cancel=cancellation.cancel,
        expected_error=SnapshotRegistrationCancellationError,
    )


async def _promote_diagnostic_snapshot(
    context: RunContext,
    registry: CompleteSnapshotRegistry,
    artifact: SnapshotArtifact,
) -> RegistrationResult:
    extra = _ui_extra(
        context,
        phase="snapshot_promotion",
        operation_type="promote",
        target="snapshot-registry",
    )
    started = time.monotonic()
    try:
        result = await _run_diagnostic_promotion(context, registry, artifact)
    except Exception as exc:
        error_type = _operation_error_type(exc)
        _record_snapshot_promotion(context, extra, started, "failed", error_type)
        raise SnapshotError(
            "UI snapshot promotion failed",
            safe_details={
                "failure_stage": "snapshot_promotion",
                "operation_type": "promote",
                "operation_target": "snapshot-registry",
                "operation_error_type": error_type,
            },
        ) from exc
    _record_snapshot_promotion(context, extra, started, "passed")
    return result


async def _run_diagnostic_promotion(
    context: RunContext,
    registry: CompleteSnapshotRegistry,
    artifact: SnapshotArtifact,
) -> RegistrationResult:
    cancellation = SnapshotRegistrationCancellation()
    return await _await_snapshot_work(
        context.services.admission.run_blocking(
            registry.promote_diagnostic,
            Path(artifact.canonical_file).name,
            artifact.activity,
            cancellation=cancellation,
        ),
        cancel=cancellation.cancel,
        expected_error=SnapshotRegistrationCancellationError,
    )


def _record_snapshot_promotion(
    context: RunContext,
    extra: dict[str, object],
    started: float,
    status: str,
    error_type: str | None = None,
) -> None:
    duration_ms = _duration_ms(started)
    _record_ui_operation(
        context,
        extra,
        status=status,
        duration_ms=duration_ms,
        error_type=error_type,
    )
    message = f"UI snapshot promotion {status}: promote target=snapshot-registry"
    log = logger.error if status == "failed" else logger.info
    log(message, extra={**extra, "duration_ms": duration_ms, "status": status})


def _mark_snapshot_artifact_verified(
    context: RunContext,
    artifacts: list[SnapshotArtifact],
    diagnostic: SnapshotArtifact,
) -> None:
    index = artifacts.index(diagnostic)
    artifacts[index] = diagnostic.model_copy(
        update={"evidence_state": "verified", "failure_stage": None}
    )
    for artifact in context.artifacts:
        if artifact.get("path") == diagnostic.canonical_file:
            artifact.update(
                kind="semantic-snapshot",
                description=f"Canonical DOM state for {diagnostic.activity}",
            )
            return
    raise SnapshotError("Verification diagnostic artifact is unavailable")


def _operation_error_type(exc: Exception) -> str:
    cause = exc.__cause__
    return type(cause).__name__ if cause is not None else type(exc).__name__


def _duration_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _snapshot_evidence(
    *,
    action_failed: bool,
    verification_expected: bool,
) -> tuple[EvidenceState, DiagnosticStage | None]:
    if action_failed:
        return "diagnostic", "action"
    if verification_expected:
        return "diagnostic", "verification"
    return "verified", None


def _cleanup_pending(pending: list[PendingSnapshot]) -> None:
    failures: list[Exception] = []
    for item in pending:
        try:
            item.staged.cleanup()
        except Exception as exc:  # noqa: BLE001 - every private stage must be attempted.
            failures.append(exc)
    if failures:
        raise SnapshotError("Unable to clean private snapshot staging data") from ExceptionGroup(
            "snapshot staging cleanup failures",
            failures,
        )


async def prepare_ui_activity(context: RunContext) -> None:
    """Provision the configured browser before per-step execution time begins."""

    browser = await context.services.browser()
    await browser.prepare()


def register_ui_activity(registry: ActivityRegistry) -> None:
    """Register the only built-in activity exposed for web UI automation."""

    registry.register(
        "capturePageSnapshot",
        CapturePageSnapshotParams,
        capture_page_snapshot,
        description=(
            "Navigate any web UI, execute semantic actions and verifications, and capture "
            "a canonical real-time DOM snapshot"
        ),
        preparer=prepare_ui_activity,
    )
