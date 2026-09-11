"""Secure, bounded Zephyr Scale Server/Data Center result reporting."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, ParamSpec, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from plantain.config import Settings
from plantain.errors import ConfigurationError
from plantain.models.scenario import ScenarioDefinition
from plantain.observability import get_logger
from plantain.reporting.base import PublicationResult
from plantain.reporting.http import BoundedHttpPoster, PostResponse, ReportingTransportError
from plantain.reporting.outbox import (
    OutboxJob,
    PublicationOutbox,
    PublicationOutboxError,
    TransitionState,
)
from plantain.security.secrets import SecretRegistry
from plantain.security.url_policy import UrlPolicy

if TYPE_CHECKING:
    from plantain.engine.admission import ResourceAdmission

logger = get_logger("reporting.zephyr")

PROVIDER: Final = "zephyr_scale_server"
TOKEN_ENVIRONMENT_NAME: Final = "JIRA_PERSONAL_ACCESS_TOKEN"  # noqa: S105 - name only.
MAX_TOKEN_LENGTH: Final = 16_384
MAX_KEY_LENGTH: Final = 128
MAX_COMMENT_LENGTH: Final = 1_000
MAX_PUBLICATION_REQUEST_BYTES: Final = 65_536
SUCCESS_STATUSES: Final = frozenset({200, 201})
ZEPHYR_RESULT_STATUSES: Final = frozenset({"Blocked", "Fail", "Pass"})
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CORRELATION_ID = re.compile(r"[0-9a-f]{32}")
_P = ParamSpec("_P")
_T = TypeVar("_T")


def _normalized_zephyr_base_url(settings: Settings) -> str:
    raw = str(getattr(settings, "zephyr_base_url", "")).strip()
    if not raw:
        raise ConfigurationError("JIRA_BASE_URL is required when Zephyr publishing is enabled")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ConfigurationError("JIRA_BASE_URL is malformed") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("JIRA_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("JIRA_BASE_URL must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("JIRA_BASE_URL must not contain a query or fragment")
    if scheme != "https" and not getattr(settings, "zephyr_allow_insecure_http", False):
        raise ConfigurationError(
            "JIRA_BASE_URL must use HTTPS unless insecure HTTP is explicitly enabled"
        )
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, normalized_path, "", ""))


def _read_zephyr_token(
    environ: Mapping[str, str],
    secrets: SecretRegistry | None = None,
) -> str:
    token = environ.get(TOKEN_ENVIRONMENT_NAME)
    if token is None or not token or token != token.strip():
        raise ConfigurationError(
            f"{TOKEN_ENVIRONMENT_NAME} is required when Zephyr publishing is enabled"
        )
    if len(token) > MAX_TOKEN_LENGTH or any(
        character in token for character in ("\r", "\n", "\x00")
    ):
        raise ConfigurationError(f"{TOKEN_ENVIRONMENT_NAME} is invalid")
    if token.casefold() == "n/a":
        raise ConfigurationError(f"{TOKEN_ENVIRONMENT_NAME} cannot be N/A")
    if secrets is not None:
        secrets.observe_environment(TOKEN_ENVIRONMENT_NAME, token)
    return token


def validate_zephyr_configuration(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Validate enabled Zephyr connection inputs without opening a connection."""

    if not bool(getattr(settings, "zephyr_publish_results", False)):
        return
    _normalized_zephyr_base_url(settings)
    _read_zephyr_token(os.environ if environ is None else environ)
    if getattr(settings, "zephyr_attach_report", False) and not getattr(
        settings,
        "zephyr_attachment_data_governance_approved",
        False,
    ):
        raise ConfigurationError(
            "Zephyr report attachment requires explicit data-governance approval"
        )


def _outbox_identity(report: Mapping[str, Any]) -> tuple[str, int]:
    job_id = report.get("correlation_id")
    created_at_ms = report.get("started_at_ms")
    if not isinstance(job_id, str) or _CORRELATION_ID.fullmatch(job_id) is None:
        raise ConfigurationError("Authoritative scenario result identity is unavailable")
    if not isinstance(created_at_ms, int) or isinstance(created_at_ms, bool) or created_at_ms < 0:
        raise ConfigurationError("Authoritative scenario start time is unavailable")
    return job_id, created_at_ms


def _validated_outbox_payload(job: OutboxJob) -> dict[str, str | int]:
    payload = job.payload
    expected = {"comment", "executionTime", "status", "testCaseKey"}
    if job.test_run_key is None:
        expected.add("projectKey")
    if set(payload) != expected or payload["testCaseKey"] != job.test_case_key:
        raise ConfigurationError("Publication outbox payload is inconsistent")
    status = payload["status"]
    comment = payload["comment"]
    duration_ms = payload["executionTime"]
    if not isinstance(status, str) or status not in ZEPHYR_RESULT_STATUSES:
        raise ConfigurationError("Publication outbox status is invalid")
    if not isinstance(comment, str) or len(comment) > MAX_COMMENT_LENGTH:
        raise ConfigurationError("Publication outbox comment is invalid")
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0:
        raise ConfigurationError("Publication outbox duration is invalid")
    if job.test_run_key is None and payload["projectKey"] != (job.test_case_key.partition("-")[0]):
        raise ConfigurationError("Publication outbox project is inconsistent")
    return dict(payload)


def _terminal_outbox_result(job: OutboxJob) -> PublicationResult | None:
    if job.state == "delivered":
        return PublicationResult(
            provider=PROVIDER,
            status="published",
            http_status=job.http_status,
            execution_id=job.execution_id,
            delivery_state="confirmed",
            retry_status="not_needed",
            outbox_id=job.job_id,
            attachment_status="not_retried",
        )
    if job.state == "rejected":
        return PublicationResult(
            provider=PROVIDER,
            status="failed",
            http_status=job.http_status,
            failure_stage=job.failure_stage or "publish_http",
            delivery_state="confirmed",
            retry_status="not_retryable",
            outbox_id=job.job_id,
            attachment_status="not_attempted",
        )
    return None


def _unsettled_outbox_result(job: OutboxJob) -> PublicationResult | None:
    if job.state == "queued":
        return None
    return PublicationResult(
        provider=PROVIDER,
        status="pending" if job.state == "sending" else "failed",
        failure_stage=job.failure_stage or f"publication_{job.state}",
        delivery_state="ambiguous",
        retry_status="manual_reconciliation",
        outbox_id=job.job_id,
        attachment_status="not_attempted",
    )


def _outbox_failure(
    job_id: str | None,
    *,
    exception_type: str | None = None,
) -> PublicationResult:
    if exception_type is not None:
        logger.error(
            "Zephyr publication outbox failed",
            extra={
                "integration": PROVIDER,
                "publication_status": "failed",
                "failure_stage": "publication_outbox",
                "exception_type": exception_type,
            },
        )
    return PublicationResult(
        provider=PROVIDER,
        status="failed",
        failure_stage="publication_outbox",
        delivery_state="not_sent",
        retry_status="manual_reconciliation",
        outbox_id=job_id,
        attachment_status="not_attempted",
    )


def _queued_outbox_result(job: OutboxJob) -> PublicationResult:
    return PublicationResult(
        provider=PROVIDER,
        status="queued",
        failure_stage=job.failure_stage,
        delivery_state="not_sent",
        retry_status="queued",
        outbox_id=job.job_id,
        attachment_status="not_attempted",
    )


def _transport_failure_result(
    error: ReportingTransportError,
    *,
    outbox_id: str | None,
    finalized: bool,
) -> PublicationResult:
    queued = error.delivery_state == "not_sent" and outbox_id is not None and finalized
    if queued:
        retry_status = "queued"
    elif error.delivery_state == "ambiguous" or not finalized:
        retry_status = "manual_reconciliation"
    else:
        retry_status = "not_configured"
    return PublicationResult(
        provider=PROVIDER,
        status="queued" if queued else "failed",
        failure_stage=error.stage if finalized else "publication_outbox",
        delivery_state=error.delivery_state,
        retry_status=retry_status,
        outbox_id=outbox_id,
        attachment_status="not_attempted",
    )


class _AttachmentTooLargeError(Exception):
    pass


class ZephyrReporter:
    """Publish results through the Zephyr Scale Server/DC v1 API."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        admission: ResourceAdmission | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._admission = admission
        self._environ = os.environ if environ is None else environ
        self._http = BoundedHttpPoster(
            timeout_seconds=getattr(settings, "zephyr_timeout_seconds", 15.0),
            max_response_bytes=getattr(settings, "zephyr_max_response_bytes", 1_048_576),
            max_connections=getattr(settings, "zephyr_max_connections", 4),
            transport=transport,
        )
        self._url_policy: UrlPolicy | None = None
        self._outbox_enabled = bool(getattr(settings, "zephyr_outbox_enabled", True))
        self._outbox: PublicationOutbox | None = None

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "zephyr_publish_results", False))

    def _publication_outbox(self) -> PublicationOutbox:
        if self._outbox is not None:
            return self._outbox
        output_dir = getattr(self._settings, "output_dir", None)
        if not isinstance(output_dir, Path):
            raise ConfigurationError(
                "Output directory is unavailable for durable Zephyr publication"
            )
        try:
            self._outbox = PublicationOutbox(
                output_dir / "reporting-outbox" / self.provider,
                max_entries=getattr(self._settings, "zephyr_outbox_max_entries", 1_000),
                lock_timeout_seconds=getattr(
                    self._settings,
                    "zephyr_outbox_lock_timeout_seconds",
                    5.0,
                ),
            )
        except ValueError as exc:
            raise ConfigurationError("Zephyr outbox configuration is invalid") from exc
        return self._outbox

    async def _run_local(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        if self._admission is not None:
            return await self._admission.run_blocking(function, *args, **kwargs)
        return await asyncio.to_thread(function, *args, **kwargs)

    async def _enqueue_outbox(
        self,
        *,
        job_id: str,
        created_at_ms: int,
        base_url: str,
        test_case_key: str,
        test_run_key: str | None,
        payload: Mapping[str, str | int],
    ) -> tuple[PublicationOutbox, OutboxJob] | PublicationResult:
        try:
            outbox = self._publication_outbox()
            job = await self._run_local(
                outbox.enqueue,
                job_id=job_id,
                base_url=base_url,
                test_case_key=test_case_key,
                test_run_key=test_run_key,
                payload=payload,
                created_at_ms=created_at_ms,
            )
        except (ConfigurationError, PublicationOutboxError) as exc:
            return _outbox_failure(job_id, exception_type=type(exc).__name__)
        existing = _terminal_outbox_result(job) or _unsettled_outbox_result(job)
        if existing is not None:
            return existing
        return outbox, job

    async def _claim_outbox(
        self,
        outbox: PublicationOutbox,
        job: OutboxJob,
    ) -> tuple[PublicationOutbox, OutboxJob] | PublicationResult:
        try:
            claimed = await self._run_local(outbox.claim, job.job_id)
            if claimed is not None:
                return outbox, claimed
            current = await self._run_local(outbox.get, job.job_id)
        except PublicationOutboxError as exc:
            return _outbox_failure(job.job_id, exception_type=type(exc).__name__)
        existing = _terminal_outbox_result(current) or _unsettled_outbox_result(current)
        if existing is not None:
            return existing
        return _queued_outbox_result(current)

    async def _prepare_outbox(
        self,
        *,
        job_id: str,
        created_at_ms: int,
        base_url: str,
        test_case_key: str,
        test_run_key: str | None,
        payload: Mapping[str, str | int],
    ) -> tuple[PublicationOutbox, OutboxJob] | PublicationResult:
        queued = await self._enqueue_outbox(
            job_id=job_id,
            created_at_ms=created_at_ms,
            base_url=base_url,
            test_case_key=test_case_key,
            test_run_key=test_run_key,
            payload=payload,
        )
        if isinstance(queued, PublicationResult):
            return queued
        return await self._claim_outbox(*queued)

    def _prepare_retry_request(
        self,
        job: OutboxJob,
        *,
        base_url: str,
    ) -> tuple[str, bytes]:
        if job.base_url != base_url:
            raise ConfigurationError("Publication outbox target is inconsistent")
        test_case_key = _safe_key(
            job.test_case_key,
            name="testCaseKey",
            required=True,
        )
        if test_case_key is None or test_case_key.casefold() == "n/a":
            raise ConfigurationError("Publication outbox test case is invalid")
        test_run_key = _safe_key(job.test_run_key, name="testRunKey", required=False)
        if test_run_key is not None and test_run_key.casefold() == "n/a":
            test_run_key = None
        endpoint = self._result_endpoint(base_url, test_case_key, test_run_key)
        payload = _validated_outbox_payload(job)
        return endpoint, _encode_json_bounded(payload, MAX_PUBLICATION_REQUEST_BYTES)

    async def _transition_outbox(
        self,
        outbox: PublicationOutbox,
        job_id: str,
        *,
        state: TransitionState,
        failure_stage: str | None = None,
        http_status: int | None = None,
        execution_id: str | None = None,
    ) -> bool:
        try:
            await self._run_local(
                outbox.transition,
                job_id,
                state=state,
                failure_stage=failure_stage,
                http_status=http_status,
                execution_id=execution_id,
            )
        except PublicationOutboxError as exc:
            _outbox_failure(job_id, exception_type=type(exc).__name__)
            return False
        return True

    async def _finish_transport_failure(
        self,
        outbox: PublicationOutbox | None,
        job_id: str | None,
        error: ReportingTransportError,
    ) -> PublicationResult:
        if outbox is None or job_id is None:
            return _transport_failure_result(error, outbox_id=None, finalized=True)
        state: TransitionState = "queued" if error.delivery_state == "not_sent" else "ambiguous"
        finalized = await self._transition_outbox(
            outbox,
            job_id,
            state=state,
            failure_stage=error.stage,
        )
        return _transport_failure_result(error, outbox_id=job_id, finalized=finalized)

    async def _finish_retried_response(
        self,
        outbox: PublicationOutbox,
        job: OutboxJob,
        response: PostResponse,
    ) -> bool:
        if response.status_code in SUCCESS_STATUSES:
            return await self._transition_outbox(
                outbox,
                job.job_id,
                state="delivered",
                http_status=response.status_code,
                execution_id=_extract_execution_id(response.content),
            )
        return await self._transition_outbox(
            outbox,
            job.job_id,
            state="rejected",
            failure_stage="publish_http",
            http_status=response.status_code,
        )

    async def _claim_retry_request(
        self,
        outbox: PublicationOutbox,
        job: OutboxJob,
        *,
        base_url: str,
    ) -> tuple[OutboxJob, str, bytes] | bool:
        claimed = await self._claim_outbox(outbox, job)
        if isinstance(claimed, PublicationResult):
            return claimed.failure_stage != "publication_outbox"
        _claimed_outbox, claimed_job = claimed
        try:
            endpoint, payload = self._prepare_retry_request(
                claimed_job,
                base_url=base_url,
            )
        except (ConfigurationError, TypeError, ValueError, _AttachmentTooLargeError):
            return await self._transition_outbox(
                outbox,
                claimed_job.job_id,
                state="rejected",
                failure_stage="outbox_validation",
            )
        return claimed_job, endpoint, payload

    async def _retry_queued_job(
        self,
        outbox: PublicationOutbox,
        job: OutboxJob,
        *,
        base_url: str,
        token: str,
        policy: UrlPolicy,
    ) -> bool:
        request = await self._claim_retry_request(
            outbox,
            job,
            base_url=base_url,
        )
        if isinstance(request, bool):
            return request
        claimed_job, endpoint, payload = request
        try:
            response = await self._http.post_json(
                endpoint,
                payload,
                token=token,
                policy=policy,
                operation="retry",
            )
        except ReportingTransportError as exc:
            await self._finish_transport_failure(outbox, claimed_job.job_id, exc)
            return False
        return await self._finish_retried_response(outbox, claimed_job, response)

    async def _drain_queued_outbox(
        self,
        outbox: PublicationOutbox,
        *,
        base_url: str,
        current_job_id: str,
        token: str,
        policy: UrlPolicy,
    ) -> None:
        limit = getattr(self._settings, "zephyr_outbox_retry_batch_size", 1)
        try:
            jobs = await self._run_local(
                outbox.queued,
                base_url=base_url,
                limit=limit,
                exclude_job_id=current_job_id,
            )
        except (ValueError, PublicationOutboxError) as exc:
            _outbox_failure(None, exception_type=type(exc).__name__)
            return
        for job in jobs:
            try:
                continued = await self._retry_queued_job(
                    outbox,
                    job,
                    base_url=base_url,
                    token=token,
                    policy=policy,
                )
            except Exception as exc:  # noqa: BLE001 - optional replay is contained.
                _outbox_failure(None, exception_type=type(exc).__name__)
                break
            if not continued:
                break

    def validate_scenario(self, scenario: ScenarioDefinition) -> None:
        """Fail before test execution when explicitly enabled reporting is unusable."""

        if not self.enabled:
            return
        validate_zephyr_configuration(self._settings, environ=self._environ)
        self._scenario_keys(scenario)

    async def publish(  # noqa: PLR0911 - delivery outcomes are explicit and non-raising.
        self,
        scenario: ScenarioDefinition,
        *,
        status: str,
        duration_ms: int,
        report: Mapping[str, Any],
        secrets: SecretRegistry,
    ) -> PublicationResult:
        """Publish a result without allowing reporting failure to mask test status."""

        if not self.enabled:
            return PublicationResult(
                provider=self.provider,
                status="disabled",
                attachment_status="disabled",
            )

        try:
            base_url = self._normalized_base_url()
            test_case_key, test_run_key = self._scenario_keys(scenario)
            token = self._read_token(secrets)
            policy = self._policy(base_url)
            endpoint = self._result_endpoint(base_url, test_case_key, test_run_key)
            payload = self._result_payload(
                test_case_key=test_case_key,
                test_run_key=test_run_key,
                status=status,
                duration_ms=duration_ms,
            )
            encoded_payload = _encode_json_bounded(payload, MAX_PUBLICATION_REQUEST_BYTES)
            outbox_identity = _outbox_identity(report) if self._outbox_enabled else None
        except ConfigurationError as exc:
            logger.error(  # noqa: TRY400 - tracebacks could expose configuration values.
                "Zephyr publication configuration is invalid",
                extra={
                    "integration": self.provider,
                    "publication_status": "failed",
                    "failure_stage": "configuration",
                    "exception_type": type(exc).__name__,
                },
            )
            return PublicationResult(
                provider=self.provider,
                status="failed",
                failure_stage="configuration",
                attachment_status="not_attempted",
            )
        except Exception as exc:  # noqa: BLE001 - security preparation fails closed.
            logger.error(  # noqa: TRY400 - tracebacks could expose prepared values.
                "Zephyr publication preparation failed",
                extra={
                    "integration": self.provider,
                    "publication_status": "failed",
                    "failure_stage": "preparation",
                    "exception_type": type(exc).__name__,
                },
            )
            return PublicationResult(
                provider=self.provider,
                status="failed",
                failure_stage="preparation",
                attachment_status="not_attempted",
            )

        outbox: PublicationOutbox | None = None
        outbox_id: str | None = None
        if outbox_identity is not None:
            job_id, created_at_ms = outbox_identity
            prepared = await self._prepare_outbox(
                job_id=job_id,
                created_at_ms=created_at_ms,
                base_url=base_url,
                test_case_key=test_case_key,
                test_run_key=test_run_key,
                payload=payload,
            )
            if isinstance(prepared, PublicationResult):
                return prepared
            outbox, claimed = prepared
            outbox_id = claimed.job_id

        logger.info(
            "Zephyr result publication started",
            extra={
                "integration": self.provider,
                "publication_status": "running",
            },
        )
        try:
            response = await self._http.post_json(
                endpoint,
                encoded_payload,
                token=token,
                policy=policy,
                operation="publish",
            )
        except ReportingTransportError as exc:
            logger.error(  # noqa: TRY400 - transport tracebacks may contain headers.
                "Zephyr result publication failed",
                extra={
                    "integration": self.provider,
                    "publication_status": "failed",
                    "failure_stage": exc.stage,
                },
            )
            return await self._finish_transport_failure(outbox, outbox_id, exc)

        if response.status_code not in SUCCESS_STATUSES:
            logger.error(
                "Zephyr result publication returned an unsuccessful HTTP status",
                extra={
                    "integration": self.provider,
                    "publication_status": "failed",
                    "failure_stage": "publish_http",
                    "http_status": response.status_code,
                },
            )
            finalized = True
            if outbox is not None and outbox_id is not None:
                finalized = await self._transition_outbox(
                    outbox,
                    outbox_id,
                    state="rejected",
                    failure_stage="publish_http",
                    http_status=response.status_code,
                )
            return PublicationResult(
                provider=self.provider,
                status="failed",
                http_status=response.status_code,
                failure_stage="publish_http" if finalized else "publication_outbox",
                delivery_state="confirmed",
                retry_status="not_retryable" if finalized else "manual_reconciliation",
                outbox_id=outbox_id,
                attachment_status="not_attempted",
            )

        execution_id = _extract_execution_id(response.content)
        finalized = True
        if outbox is not None and outbox_id is not None:
            finalized = await self._transition_outbox(
                outbox,
                outbox_id,
                state="delivered",
                http_status=response.status_code,
                execution_id=execution_id,
            )
        attachment_status, attachment_http_status = await self._attach_if_enabled(
            base_url=base_url,
            execution_id=execution_id,
            report=report,
            token=token,
            policy=policy,
        )
        if finalized and outbox is not None and outbox_id is not None:
            await self._drain_queued_outbox(
                outbox,
                base_url=base_url,
                current_job_id=outbox_id,
                token=token,
                policy=policy,
            )
        logger.info(
            "Zephyr result publication completed",
            extra={
                "integration": self.provider,
                "publication_status": "published",
                "attachment_status": attachment_status,
                "http_status": response.status_code,
            },
        )
        return PublicationResult(
            provider=self.provider,
            status="published",
            http_status=response.status_code,
            execution_id=execution_id,
            failure_stage=None if finalized else "publication_outbox",
            delivery_state="confirmed",
            retry_status="not_needed" if finalized else "manual_reconciliation",
            outbox_id=outbox_id,
            attachment_status=attachment_status,
            attachment_http_status=attachment_http_status,
        )

    async def close(self) -> None:
        await self._http.close()

    async def _attach_if_enabled(  # noqa: PLR0911 - each nonfatal outcome is explicit.
        self,
        *,
        base_url: str,
        execution_id: str | None,
        report: Mapping[str, Any],
        token: str,
        policy: UrlPolicy,
    ) -> tuple[str, int | None]:
        if not getattr(self._settings, "zephyr_attach_report", False):
            return "disabled", None
        if not getattr(
            self._settings,
            "zephyr_attachment_data_governance_approved",
            False,
        ):
            return "blocked_governance", None
        if execution_id is None:
            return "skipped_no_execution_id", None
        try:
            report_bytes = _encode_json_bounded(
                report,
                getattr(self._settings, "zephyr_max_attachment_bytes", 5_242_880),
            )
        except _AttachmentTooLargeError:
            return "skipped_too_large", None
        except (TypeError, ValueError):
            return "failed", None

        endpoint = f"{base_url}/rest/atm/1.0/testresult/{quote(execution_id, safe='')}/attachments"
        try:
            response = await self._http.post_file(
                endpoint,
                report_bytes,
                token=token,
                policy=policy,
                operation="attachment",
            )
        except ReportingTransportError as exc:
            logger.error(  # noqa: TRY400 - transport tracebacks may contain headers.
                "Zephyr report attachment failed",
                extra={
                    "integration": self.provider,
                    "attachment_status": "failed",
                    "failure_stage": exc.stage,
                },
            )
            return "failed", None
        if response.status_code not in SUCCESS_STATUSES:
            logger.error(
                "Zephyr report attachment returned an unsuccessful HTTP status",
                extra={
                    "integration": self.provider,
                    "attachment_status": "failed",
                    "failure_stage": "attachment_http",
                    "http_status": response.status_code,
                },
            )
            return "failed", response.status_code
        return "attached", response.status_code

    def _normalized_base_url(self) -> str:
        return _normalized_zephyr_base_url(self._settings)

    def _policy(self, base_url: str) -> UrlPolicy:
        if self._url_policy is None:
            hostname = urlsplit(base_url).hostname
            if hostname is None:
                raise ConfigurationError("JIRA_BASE_URL must include a hostname")
            self._url_policy = UrlPolicy(
                environment=getattr(self._settings, "environment", "production"),
                network_mode="restricted",
                allow_private_networks=getattr(
                    self._settings,
                    "zephyr_allow_private_networks",
                    False,
                ),
                allowed_hosts=(hostname.casefold(),),
                allow_insecure_local_http=getattr(
                    self._settings,
                    "zephyr_allow_insecure_http",
                    False,
                ),
                egress_control_enforced=getattr(
                    self._settings,
                    "egress_control_enforced",
                    False,
                ),
                admission=self._admission,
            )
        return self._url_policy

    @staticmethod
    def _scenario_keys(scenario: ScenarioDefinition) -> tuple[str, str | None]:
        test_case_key = _safe_key(scenario.test_case_key, name="testCaseKey", required=True)
        if test_case_key is None or test_case_key.casefold() == "n/a":
            raise ConfigurationError(
                "testCaseKey must identify a Zephyr test case when publishing is enabled"
            )
        jira_ticket = scenario.jira_ticket
        if jira_ticket is not None and any(
            character in jira_ticket for character in ("\r", "\n", "\x00")
        ):
            raise ConfigurationError("JiraTicket contains invalid control characters")
        test_run_key = _safe_key(scenario.test_run_key, name="testRunKey", required=False)
        if test_run_key is not None and test_run_key.casefold() == "n/a":
            test_run_key = None
        return test_case_key, test_run_key

    @staticmethod
    def _result_endpoint(base_url: str, test_case_key: str, test_run_key: str | None) -> str:
        api_root = f"{base_url}/rest/atm/1.0"
        if test_run_key is None:
            return f"{api_root}/testresult"
        return (
            f"{api_root}/testrun/{quote(test_run_key, safe='')}/testcase/"
            f"{quote(test_case_key, safe='')}/testresult"
        )

    @staticmethod
    def _result_payload(
        *,
        test_case_key: str,
        test_run_key: str | None,
        status: str,
        duration_ms: int,
    ) -> dict[str, str | int]:
        comment = f"Plantain automated result: {status.casefold()}"[:MAX_COMMENT_LENGTH]
        payload: dict[str, str | int] = {
            "testCaseKey": test_case_key,
            "status": _zephyr_status(status),
            "comment": comment,
            "executionTime": max(duration_ms, 0),
        }
        if test_run_key is None:
            payload["projectKey"] = test_case_key.partition("-")[0]
        return payload

    def _read_token(self, secrets: SecretRegistry | None = None) -> str:
        return _read_zephyr_token(self._environ, secrets)


def _safe_key(value: str | int | None, *, name: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise ConfigurationError(f"{name} is required when Zephyr publishing is enabled")
        return None
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a safe Zephyr key")
    rendered = str(value).strip()
    if not rendered:
        if required:
            raise ConfigurationError(f"{name} is required when Zephyr publishing is enabled")
        return None
    if rendered.casefold() == "n/a":
        return rendered
    if len(rendered) > MAX_KEY_LENGTH or SAFE_KEY.fullmatch(rendered) is None:
        raise ConfigurationError(f"{name} must be a safe Zephyr key")
    return rendered


def _zephyr_status(status: str) -> str:
    return {
        "passed": "Pass",
        "failed": "Fail",
        "skipped": "Blocked",
    }.get(status.casefold(), "Not Executed")


def _encode_json_bounded(value: object, limit: int) -> bytes:
    buffer = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    for fragment in encoder.iterencode(value):
        encoded = fragment.encode("utf-8")
        if len(buffer) + len(encoded) > limit:
            raise _AttachmentTooLargeError
        buffer.extend(encoded)
    return bytes(buffer)


def _extract_execution_id(content: bytes) -> str | None:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        if not payload:
            return None
        payload = payload[0]
    if not isinstance(payload, dict):
        return None
    value = payload.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    rendered = str(value).strip()
    if len(rendered) > MAX_KEY_LENGTH or SAFE_KEY.fullmatch(rendered) is None:
        return None
    return rendered


__all__ = ["PROVIDER", "ZephyrReporter", "validate_zephyr_configuration"]
