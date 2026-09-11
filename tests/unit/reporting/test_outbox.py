"""Durable at-most-once publication outbox tests."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from plantain import persistence
from plantain.errors import AtomicCommitUncertainError
from plantain.reporting import outbox as outbox_module
from plantain.reporting.outbox import (
    MAX_OUTBOX_DOCUMENT_BYTES,
    PublicationOutbox,
    PublicationOutboxCommitUncertainError,
    PublicationOutboxError,
)

BASE_URL = "https://jira.example.test"
JOB_A = "a" * 32
JOB_B = "b" * 32
JOB_C = "c" * 32
JOB_D = "d" * 32
SECOND_ATTEMPT = 2
HTTP_CREATED = 201


def _payload(comment: str = "Scenario passed") -> dict[str, str | int]:
    return {
        "testCaseKey": "QA-T42",
        "status": "Pass",
        "comment": comment,
        "executionTime": 25,
    }


def _enqueue(
    outbox: PublicationOutbox,
    job_id: str = JOB_A,
    *,
    payload: Mapping[str, str | int] | None = None,
    created_at_ms: int = 1,
) -> None:
    outbox.enqueue(
        job_id=job_id,
        base_url=BASE_URL,
        test_case_key="QA-T42",
        test_run_key="QA-R7",
        payload=payload or _payload(),
        created_at_ms=created_at_ms,
    )


def test_outbox_lifecycle_is_idempotent_and_only_queued_jobs_are_retryable(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "outbox"
    outbox = PublicationOutbox(directory)
    _enqueue(outbox)
    _enqueue(outbox)

    assert [job.job_id for job in outbox.queued(base_url=BASE_URL, limit=1)] == [JOB_A]
    claimed = outbox.claim(JOB_A)
    assert claimed is not None
    assert claimed.state == "sending"
    assert claimed.attempts == 1
    assert PublicationOutbox(directory).claim(JOB_A) is None

    queued = outbox.transition(
        JOB_A,
        state="queued",
        failure_stage="publish_network",
    )
    assert queued.state == "queued"
    claimed_again = outbox.claim(JOB_A)
    assert claimed_again is not None
    assert claimed_again.attempts == SECOND_ATTEMPT
    delivered = outbox.transition(
        JOB_A,
        state="delivered",
        http_status=HTTP_CREATED,
        execution_id="QA-E1",
    )
    assert (
        outbox.transition(
            JOB_A,
            state="delivered",
            http_status=HTTP_CREATED,
            execution_id="QA-E1",
        )
        == delivered
    )
    assert delivered.http_status == HTTP_CREATED
    assert delivered.execution_id == "QA-E1"
    assert outbox.queued(base_url=BASE_URL, limit=1) == ()

    job_path = directory / f"{JOB_A}.json"
    if os.name != "nt":
        job_path.chmod(0o666)
        assert outbox.get(JOB_A) == delivered
        assert stat.S_IMODE(job_path.stat().st_mode) == persistence.PRIVATE_FILE_MODE
    document = json.loads(job_path.read_text(encoding="utf-8"))
    assert document["state"] == "delivered"
    assert "token" not in document
    if os.name != "nt":
        assert stat.S_IMODE(directory.stat().st_mode) == persistence.PRIVATE_DIRECTORY_MODE
        assert (
            stat.S_IMODE((directory / ".outbox.lock").stat().st_mode)
            == persistence.PRIVATE_FILE_MODE
        )
        assert (
            stat.S_IMODE((directory / f"{JOB_A}.json").stat().st_mode)
            == persistence.PRIVATE_FILE_MODE
        )


def test_outbox_rejects_conflicting_identity_and_tampered_content(tmp_path: Path) -> None:
    directory = tmp_path / "outbox"
    outbox = PublicationOutbox(directory)
    _enqueue(outbox)

    with pytest.raises(PublicationOutboxError, match="conflicts"):
        _enqueue(outbox, payload=_payload("Different intent"))

    path = directory / f"{JOB_A}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["comment"] = "tampered"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PublicationOutboxError, match="integrity"):
        outbox.get(JOB_A)


def test_outbox_prunes_only_terminal_jobs_and_fails_closed_at_active_capacity(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "outbox"
    outbox = PublicationOutbox(directory, max_entries=2)
    _enqueue(outbox, JOB_A, created_at_ms=1)
    assert outbox.claim(JOB_A) is not None
    outbox.transition(JOB_A, state="delivered")
    _enqueue(outbox, JOB_B, created_at_ms=2)
    _enqueue(outbox, JOB_C, created_at_ms=3)

    assert not (directory / f"{JOB_A}.json").exists()
    assert (directory / f"{JOB_B}.json").exists()
    assert (directory / f"{JOB_C}.json").exists()
    with pytest.raises(PublicationOutboxError, match="no safely prunable capacity"):
        _enqueue(outbox, JOB_D, created_at_ms=4)


def test_outbox_rejects_oversized_intent_before_persistence(tmp_path: Path) -> None:
    outbox = PublicationOutbox(tmp_path / "outbox")

    with pytest.raises(PublicationOutboxError, match="byte limit"):
        _enqueue(
            outbox,
            payload=_payload("x" * MAX_OUTBOX_DOCUMENT_BYTES),
        )


def test_outbox_preserves_atomic_commit_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def uncertain_write(_path: Path, _value: Mapping[str, Any]) -> None:
        raise AtomicCommitUncertainError("safe synthetic uncertainty")

    monkeypatch.setattr(outbox_module, "write_json_atomic", uncertain_write)
    outbox = PublicationOutbox(tmp_path / "outbox")

    with pytest.raises(PublicationOutboxCommitUncertainError, match="durability"):
        _enqueue(outbox)


def test_outbox_pruning_failure_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = PublicationOutbox(tmp_path / "outbox", max_entries=1)
    _enqueue(outbox)
    assert outbox.claim(JOB_A) is not None
    outbox.transition(JOB_A, state="delivered")

    def reject_delete(_path: Path, *, missing_ok: bool) -> None:
        del missing_ok
        raise persistence.AtomicPersistenceError("sensitive filesystem detail")

    monkeypatch.setattr(outbox_module, "unlink_durable", reject_delete)
    with pytest.raises(PublicationOutboxError, match="pruning failed") as captured:
        _enqueue(outbox, JOB_B)
    assert "sensitive filesystem detail" not in str(captured.value)
