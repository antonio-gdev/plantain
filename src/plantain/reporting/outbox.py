"""Bounded durable state for at-most-once remote result publication."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from filelock import Timeout as FileLockTimeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from plantain.errors import AtomicCommitUncertainError, AtomicPersistenceError
from plantain.persistence import (
    ensure_private_directory,
    ensure_private_file,
    private_file_lock,
    unlink_durable,
    write_json_atomic,
)

OUTBOX_SCHEMA_VERSION = "1.0"
DEFAULT_MAX_OUTBOX_ENTRIES = 1_000
MAX_OUTBOX_ENTRIES = 100_000
MAX_OUTBOX_DOCUMENT_BYTES = 131_072
MAX_OUTBOX_ATTEMPTS = 1_000_000
MAX_LOCK_TIMEOUT_SECONDS = 60.0
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_TERMINAL_STATES = frozenset({"delivered", "rejected"})
_TRANSITION_STATES = frozenset({"ambiguous", "delivered", "queued", "rejected"})

OutboxState = Literal["ambiguous", "delivered", "queued", "rejected", "sending"]
TransitionState = Literal["ambiguous", "delivered", "queued", "rejected"]


class PublicationOutboxError(Exception):
    """Safe local outbox failure with no paths, payloads, or credentials."""


class PublicationOutboxCommitUncertainError(PublicationOutboxError):
    """An outbox file was replaced but its directory durability is uncertain."""


class OutboxJob(BaseModel):
    """Strict token-free Zephyr publication intent and lifecycle state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = Field(alias="schemaVersion")
    provider: Literal["zephyr_scale_server"]
    job_id: str = Field(alias="jobId", min_length=32, max_length=32)
    base_url: str = Field(alias="baseUrl", min_length=1, max_length=8_192)
    test_case_key: str = Field(alias="testCaseKey", min_length=1, max_length=128)
    test_run_key: str | None = Field(
        default=None,
        alias="testRunKey",
        max_length=128,
    )
    payload: dict[str, str | int] = Field(min_length=1, max_length=8)
    state: OutboxState
    attempts: int = Field(ge=0, le=MAX_OUTBOX_ATTEMPTS)
    failure_stage: str | None = Field(
        default=None,
        alias="failureStage",
        max_length=128,
    )
    http_status: int | None = Field(
        default=None,
        alias="httpStatus",
        ge=100,
        le=599,
    )
    execution_id: str | None = Field(
        default=None,
        alias="executionId",
        max_length=128,
    )
    created_at_ms: int = Field(alias="createdAtMs", ge=0)
    updated_at_ms: int = Field(alias="updatedAtMs", ge=0)
    digest: str = Field(min_length=64, max_length=64)


class PublicationOutbox:
    """Persist and claim bounded publication jobs under one cross-process lock."""

    def __init__(
        self,
        directory: Path,
        *,
        max_entries: int = DEFAULT_MAX_OUTBOX_ENTRIES,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or max_entries < 1
            or max_entries > MAX_OUTBOX_ENTRIES
        ):
            raise ValueError("max_entries is outside the supported outbox range")
        if (
            not isinstance(lock_timeout_seconds, int | float)
            or isinstance(lock_timeout_seconds, bool)
            or lock_timeout_seconds <= 0
            or lock_timeout_seconds > MAX_LOCK_TIMEOUT_SECONDS
        ):
            raise ValueError("lock_timeout_seconds is outside the supported outbox range")
        self._directory = directory.absolute()
        self._lock_path = self._directory / ".outbox.lock"
        self._max_entries = max_entries
        self._lock_timeout_seconds = float(lock_timeout_seconds)

    def enqueue(
        self,
        *,
        job_id: str,
        base_url: str,
        test_case_key: str,
        test_run_key: str | None,
        payload: Mapping[str, str | int],
        created_at_ms: int,
    ) -> OutboxJob:
        candidate = _new_job(
            job_id=job_id,
            base_url=base_url,
            test_case_key=test_case_key,
            test_run_key=test_run_key,
            payload=payload,
            created_at_ms=created_at_ms,
        )
        with self._locked():
            target = self._job_path(job_id)
            if target.exists():
                current = self._load(target)
                if _intent(current) != _intent(candidate):
                    raise PublicationOutboxError(
                        "Outbox job identifier conflicts with prior intent"
                    )
                return current
            self._make_capacity()
            self._write(target, candidate)
        return candidate

    def queued(
        self,
        *,
        base_url: str,
        limit: int,
        exclude_job_id: str | None = None,
    ) -> tuple[OutboxJob, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._locked():
            jobs = [self._load(path) for path in self._job_paths()]
        eligible = (
            job
            for job in jobs
            if job.state == "queued" and job.base_url == base_url and job.job_id != exclude_job_id
        )
        ordered = sorted(eligible, key=lambda job: (job.created_at_ms, job.job_id))
        return tuple(ordered[: min(limit, self._max_entries)])

    def get(self, job_id: str) -> OutboxJob:
        with self._locked():
            return self._load(self._job_path(job_id))

    def claim(self, job_id: str) -> OutboxJob | None:
        with self._locked():
            path = self._job_path(job_id)
            current = self._load(path)
            if current.state != "queued":
                return None
            claimed = _updated_job(
                current,
                state="sending",
                attempts=current.attempts + 1,
                failure_stage=None,
            )
            self._write(path, claimed)
            return claimed

    def transition(
        self,
        job_id: str,
        *,
        state: TransitionState,
        failure_stage: str | None = None,
        http_status: int | None = None,
        execution_id: str | None = None,
    ) -> OutboxJob:
        if state not in _TRANSITION_STATES:
            raise ValueError("Unsupported outbox transition state")
        with self._locked():
            path = self._job_path(job_id)
            current = self._load(path)
            if (
                current.state == state
                and current.failure_stage == failure_stage
                and current.http_status == http_status
                and current.execution_id == execution_id
            ):
                return current
            if current.state != "sending":
                raise PublicationOutboxError("Outbox job is not in the sending state")
            updated = _updated_job(
                current,
                state=state,
                attempts=current.attempts,
                failure_stage=failure_stage,
                http_status=http_status,
                execution_id=execution_id,
            )
            self._write(path, updated)
            return updated

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._prepare_directory()
        try:
            with private_file_lock(
                self._lock_path,
                timeout=self._lock_timeout_seconds,
            ):
                yield
        except FileLockTimeout as exc:
            raise PublicationOutboxError("Publication outbox lock timed out") from exc
        except (OSError, AtomicPersistenceError) as exc:
            raise PublicationOutboxError("Publication outbox lock failed") from exc

    def _prepare_directory(self) -> None:
        try:
            ensure_private_directory(self._directory)
        except (OSError, AtomicPersistenceError) as exc:
            raise PublicationOutboxError("Publication outbox directory is unavailable") from exc

    def _job_path(self, job_id: str) -> Path:
        if _JOB_ID.fullmatch(job_id) is None:
            raise PublicationOutboxError("Publication outbox job identifier is invalid")
        return self._directory / f"{job_id}.json"

    def _job_paths(self) -> list[Path]:
        paths: list[Path] = []
        entry_limit = self._max_entries + 16
        try:
            for entry_count, path in enumerate(self._directory.iterdir(), start=1):
                if entry_count > entry_limit:
                    raise PublicationOutboxError("Publication outbox directory exceeds its limit")
                if path.suffix == ".json":
                    if path.is_symlink() or not path.is_file():
                        raise PublicationOutboxError("Publication outbox entry is unsafe")
                    paths.append(path)
        except OSError as exc:
            raise PublicationOutboxError("Publication outbox directory cannot be read") from exc
        if len(paths) > self._max_entries:
            raise PublicationOutboxError("Publication outbox exceeds its entry limit")
        return paths

    def _make_capacity(self) -> None:
        paths = self._job_paths()
        if len(paths) < self._max_entries:
            return
        jobs = sorted(
            (self._load(path) for path in paths),
            key=lambda job: (job.updated_at_ms, job.job_id),
        )
        terminal = next((job for job in jobs if job.state in _TERMINAL_STATES), None)
        if terminal is None:
            raise PublicationOutboxError("Publication outbox has no safely prunable capacity")
        try:
            unlink_durable(self._job_path(terminal.job_id), missing_ok=False)
        except AtomicPersistenceError as exc:
            raise PublicationOutboxError("Publication outbox pruning failed") from exc

    def _load(self, path: Path) -> OutboxJob:
        try:
            ensure_private_file(path)
            with path.open("rb") as handle:
                raw = handle.read(MAX_OUTBOX_DOCUMENT_BYTES + 1)
        except (OSError, AtomicPersistenceError) as exc:
            raise PublicationOutboxError("Publication outbox entry cannot be read") from exc
        if len(raw) > MAX_OUTBOX_DOCUMENT_BYTES:
            raise PublicationOutboxError("Publication outbox entry exceeds its byte limit")
        try:
            value = json.loads(raw)
            job = OutboxJob.model_validate(value)
        except (
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValidationError,
        ) as exc:
            raise PublicationOutboxError("Publication outbox entry is invalid") from exc
        if _JOB_ID.fullmatch(job.job_id) is None or path.name != f"{job.job_id}.json":
            raise PublicationOutboxError("Publication outbox entry identity is invalid")
        expected = _document_digest(job.model_dump(mode="json", by_alias=True, exclude={"digest"}))
        if not hmac.compare_digest(job.digest, expected):
            raise PublicationOutboxError("Publication outbox entry integrity check failed")
        return job

    @staticmethod
    def _write(path: Path, job: OutboxJob) -> None:
        try:
            write_json_atomic(path, job.model_dump(mode="json", by_alias=True))
        except AtomicCommitUncertainError as exc:
            raise PublicationOutboxCommitUncertainError(
                "Publication outbox commit durability is uncertain"
            ) from exc
        except AtomicPersistenceError as exc:
            raise PublicationOutboxError("Publication outbox persistence failed") from exc


def _new_job(
    *,
    job_id: str,
    base_url: str,
    test_case_key: str,
    test_run_key: str | None,
    payload: Mapping[str, str | int],
    created_at_ms: int,
) -> OutboxJob:
    value: dict[str, Any] = {
        "schemaVersion": OUTBOX_SCHEMA_VERSION,
        "provider": "zephyr_scale_server",
        "jobId": job_id,
        "baseUrl": base_url,
        "testCaseKey": test_case_key,
        "testRunKey": test_run_key,
        "payload": dict(payload),
        "state": "queued",
        "attempts": 0,
        "failureStage": None,
        "httpStatus": None,
        "executionId": None,
        "createdAtMs": created_at_ms,
        "updatedAtMs": created_at_ms,
    }
    return _validated_with_digest(value)


def _updated_job(
    job: OutboxJob,
    *,
    state: OutboxState,
    attempts: int,
    failure_stage: str | None,
    http_status: int | None = None,
    execution_id: str | None = None,
) -> OutboxJob:
    value = job.model_dump(mode="json", by_alias=True, exclude={"digest"})
    value.update(
        {
            "state": state,
            "attempts": attempts,
            "failureStage": failure_stage,
            "httpStatus": http_status,
            "executionId": execution_id,
            "updatedAtMs": time.time_ns() // 1_000_000,
        }
    )
    return _validated_with_digest(value)


def _validated_with_digest(value: Mapping[str, Any]) -> OutboxJob:
    document = dict(value)
    try:
        document["digest"] = _document_digest(document)
        job = OutboxJob.model_validate(document)
        encoded = json.dumps(
            job.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PublicationOutboxError("Publication outbox intent is invalid") from exc
    if len(encoded) > MAX_OUTBOX_DOCUMENT_BYTES:
        raise PublicationOutboxError("Publication outbox entry exceeds its byte limit")
    return job


def _document_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _intent(job: OutboxJob) -> tuple[object, ...]:
    return (
        job.provider,
        job.base_url,
        job.test_case_key,
        job.test_run_key,
        job.payload,
        job.created_at_ms,
    )


__all__ = [
    "DEFAULT_MAX_OUTBOX_ENTRIES",
    "MAX_OUTBOX_ENTRIES",
    "OUTBOX_SCHEMA_VERSION",
    "OutboxJob",
    "PublicationOutbox",
    "PublicationOutboxCommitUncertainError",
    "PublicationOutboxError",
]
