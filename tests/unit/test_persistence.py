"""Durable, fail-closed persistence contract tests."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from plantain import persistence
from plantain.errors import (
    AtomicCommitUncertainError,
    AtomicPersistenceError,
    AtomicTargetExistsError,
)

SHARED_FILE_MODE = 0o644
SHARED_DIRECTORY_MODE = 0o775


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
def test_atomic_write_creates_and_repairs_owner_only_paths(tmp_path: Path) -> None:
    private_root = tmp_path / "output"
    private_root.mkdir(mode=0o777)
    private_root.chmod(0o777)
    persistence.ensure_private_directory(private_root)
    target = private_root / "nested" / "result.json"

    persistence.write_json_atomic(target, {"status": "passed"})

    assert stat.S_IMODE(private_root.stat().st_mode) == persistence.PRIVATE_DIRECTORY_MODE
    assert stat.S_IMODE(target.parent.stat().st_mode) == persistence.PRIVATE_DIRECTORY_MODE
    assert stat.S_IMODE(target.stat().st_mode) == persistence.PRIVATE_FILE_MODE

    target.chmod(0o666)
    persistence.write_json_atomic(target, {"status": "repaired"})

    assert stat.S_IMODE(target.stat().st_mode) == persistence.PRIVATE_FILE_MODE


def test_private_directory_rejects_symlinked_parent(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(
        AtomicPersistenceError,
        match="Private runtime path contains a symbolic link",
    ):
        persistence.ensure_private_directory(linked / "nested")

    assert not (actual / "nested").exists()


def test_private_directory_rejects_foreign_ownership_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "output"
    target.mkdir()
    monkeypatch.setattr(persistence, "_is_owned_by_current_user", lambda _metadata: False)

    with pytest.raises(AtomicPersistenceError) as raised:
        persistence.ensure_private_directory(target)

    assert "not owned by the current user" in str(raised.value)
    assert str(target) not in str(raised.value)


def test_private_runtime_rejects_unsupported_native_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(persistence.sys, "platform", "win32")

    with pytest.raises(
        AtomicPersistenceError,
        match="Linux, macOS, or Windows through WSL2",
    ):
        persistence.ensure_private_directory(tmp_path / "output")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
def test_private_lock_and_exclusive_file_are_owner_only(tmp_path: Path) -> None:
    lock_path = tmp_path / "locks" / "registry.lock"
    lock_path.parent.mkdir()
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o666)

    with persistence.private_file_lock(lock_path, timeout=1):
        assert stat.S_IMODE(lock_path.stat().st_mode) == persistence.PRIVATE_FILE_MODE

    assert lock_path.exists()
    assert stat.S_IMODE(lock_path.parent.stat().st_mode) == persistence.PRIVATE_DIRECTORY_MODE
    target = tmp_path / "staging" / "chunk.jsonl"
    with persistence.open_private_binary_exclusive(target) as handle:
        handle.write(b"private evidence")
    assert target.read_bytes() == b"private evidence"
    assert stat.S_IMODE(target.stat().st_mode) == persistence.PRIVATE_FILE_MODE

    with pytest.raises(
        AtomicPersistenceError,
        match="Private runtime file could not be created",
    ):
        persistence.open_private_binary_exclusive(target)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor and permission behavior")
def test_no_follow_reader_repairs_private_mode_and_holds_open_inode(tmp_path: Path) -> None:
    target = tmp_path / "private.json"
    target.write_bytes(b"original")
    target.chmod(0o666)

    with persistence.open_binary_read_no_follow(target, private=True) as handle:
        assert stat.S_IMODE(target.stat().st_mode) == persistence.PRIVATE_FILE_MODE
        target.unlink()
        target.write_bytes(b"replacement")
        assert handle.read() == b"original"


def test_no_follow_reader_rejects_symlinked_parent(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "payload.json").write_bytes(b"{}")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with (
        pytest.raises(AtomicPersistenceError, match="could not be opened safely"),
        persistence.open_binary_read_no_follow(linked / "payload.json"),
    ):
        pass


def test_atomic_primitives_write_copy_and_hash_without_partial_files(
    tmp_path: Path,
) -> None:
    json_target = tmp_path / "nested/report.json"
    persistence.write_json_atomic(json_target, {"status": "passed", "count": 2})

    assert json.loads(json_target.read_text(encoding="utf-8")) == {
        "status": "passed",
        "count": 2,
    }
    assert json_target.read_bytes().endswith(b"\n")

    payload = b"bounded snapshot payload"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    destination = tmp_path / "archive/copy.bin"

    persistence.copy_atomic(source, destination)

    assert destination.read_bytes() == payload
    assert persistence.digest_file(destination) == hashlib.sha256(payload).hexdigest()
    persistence.unlink_durable(destination)
    assert not destination.exists()
    persistence.unlink_durable(destination, missing_ok=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor and permission behavior")
def test_atomic_create_never_replaces_or_repairs_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "generated" / "scenario.yaml"
    persistence.write_bytes_atomic_new(target, b"first")

    assert target.read_bytes() == b"first"
    assert stat.S_IMODE(target.stat().st_mode) == persistence.PRIVATE_FILE_MODE
    target.chmod(SHARED_FILE_MODE)

    with pytest.raises(AtomicTargetExistsError, match="already exists") as raised:
        persistence.write_bytes_atomic_new(target, b"replacement")

    assert target.read_bytes() == b"first"
    assert stat.S_IMODE(target.stat().st_mode) == SHARED_FILE_MODE
    assert str(target) not in str(raised.value)
    assert list(target.parent.glob(".scenario.yaml.*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor and permission behavior")
def test_atomic_source_create_preserves_existing_directory_mode(tmp_path: Path) -> None:
    source_directory = tmp_path / "scenarios" / "payments"
    source_directory.mkdir(parents=True)
    source_directory.chmod(SHARED_DIRECTORY_MODE)
    target = source_directory / "checkout.yaml"

    persistence.write_source_bytes_atomic_new(target, b"scenario: Checkout\n")

    assert target.read_bytes() == b"scenario: Checkout\n"
    assert stat.S_IMODE(source_directory.stat().st_mode) == SHARED_DIRECTORY_MODE
    assert stat.S_IMODE(target.stat().st_mode) == persistence.SOURCE_FILE_MODE
    with pytest.raises(AtomicTargetExistsError):
        persistence.write_source_bytes_atomic_new(target, b"replacement")
    assert target.read_bytes() == b"scenario: Checkout\n"


def test_private_adoption_and_unlink_reject_unsafe_targets(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "trace.partial"
    with persistence.open_private_binary_exclusive(staging) as handle:
        handle.write(b"private trace")
    target = tmp_path / "traces" / "trace_1.zip"

    persistence.adopt_private_file(staging, target)

    assert not staging.exists()
    assert target.read_bytes() == b"private trace"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == persistence.PRIVATE_FILE_MODE
    persistence.unlink_private_durable(target)
    assert not target.exists()

    actual = tmp_path / "actual.zip"
    actual.write_bytes(b"keep")
    target.symlink_to(actual)
    with pytest.raises(AtomicPersistenceError, match="symbolic link"):
        persistence.unlink_private_durable(target)
    assert actual.exists()


def test_atomic_replace_failure_removes_staging_file_without_leaking_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "private" / "result.json"

    def reject_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated unsupported atomic replacement")

    monkeypatch.setattr(persistence.os, "replace", reject_replace)

    with pytest.raises(AtomicPersistenceError) as raised:
        persistence.write_json_atomic(target, {"token": "must-not-appear"})

    assert "Atomic JSON persistence failed" in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    assert "must-not-appear" not in str(raised.value)
    assert not isinstance(raised.value, AtomicCommitUncertainError)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_disk_full_write_removes_private_stage_and_hides_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "private" / "result.json"

    def reject_dump(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "synthetic disk detail")

    monkeypatch.setattr(persistence.json, "dump", reject_dump)
    with pytest.raises(AtomicPersistenceError, match="Atomic JSON persistence failed") as raised:
        persistence.write_json_atomic(target, {"private": "must-not-appear"})

    assert "synthetic disk detail" not in str(raised.value)
    assert "must-not-appear" not in str(raised.value)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_read_only_destination_fails_before_artifact_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "private" / "result.json"

    def reject_mkstemp(**_kwargs: object) -> tuple[int, str]:
        raise PermissionError(errno.EACCES, "synthetic permission detail")

    monkeypatch.setattr(persistence.tempfile, "mkstemp", reject_mkstemp)
    with pytest.raises(AtomicPersistenceError, match="staging could not be created") as raised:
        persistence.write_json_atomic(target, {"private": "must-not-appear"})

    assert "synthetic permission detail" not in str(raised.value)
    assert "must-not-appear" not in str(raised.value)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_directory_sync_failure_reports_replaced_target_as_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "private" / "result.json"
    target.parent.mkdir()
    target.write_text('{"status":"old"}', encoding="utf-8")

    def reject_sync(_directory: Path) -> None:
        raise OSError("synthetic directory detail")

    monkeypatch.setattr(persistence, "_sync_directory", reject_sync)
    with pytest.raises(AtomicCommitUncertainError) as raised:
        persistence.write_json_atomic(target, {"status": "new"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "new"}
    assert "target replacement completed" in str(raised.value)
    assert "synthetic directory detail" not in str(raised.value)
    stages = [path for path in target.parent.iterdir() if path.name.startswith(f".{target.name}.")]
    assert stages == []


def test_durable_unlink_reports_directory_sync_failure_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "private.bin"
    target.write_bytes(b"private")

    def reject_sync(_directory: Path) -> None:
        raise OSError("synthetic directory detail")

    monkeypatch.setattr(persistence, "_sync_directory", reject_sync)
    with pytest.raises(AtomicPersistenceError, match="Durable file deletion failed") as raised:
        persistence.unlink_durable(target)

    assert not target.exists()
    assert "synthetic directory detail" not in str(raised.value)
