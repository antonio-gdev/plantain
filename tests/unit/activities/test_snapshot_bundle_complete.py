"""Complete snapshot staging and persistence-adapter coverage."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from plantain.activities import snapshot_bundle, snapshot_persistence
from plantain.activities.snapshot_bundle import (
    SnapshotBundleBuilder,
    StagedSnapshot,
    _text_chunks,
)
from plantain.activities.ui_errors import SnapshotError
from plantain.errors import AtomicPersistenceError

TEXT_CHUNK_LIMIT = 3


def _complete_manifest() -> dict[str, Any]:
    return {"schemaVersion": "3.0", "captureComplete": True, "content": {}}


def test_staged_cleanup_is_idempotent_and_contained(tmp_path: Path) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")
    staged = builder.finish(_complete_manifest())

    staged.cleanup()
    staged.cleanup()

    assert not staged.staging_dir.exists()


def test_staged_cleanup_rejects_escape_and_root_removal(tmp_path: Path) -> None:
    root = tmp_path / "snapshots" / ".staging"
    root.mkdir(parents=True)
    outside = StagedSnapshot(
        manifest={},
        chunks=(),
        staging_dir=tmp_path / "outside",
        staging_root=root,
    )
    root_stage = StagedSnapshot(
        manifest={},
        chunks=(),
        staging_dir=root,
        staging_root=root,
    )

    with pytest.raises(SnapshotError, match="escapes its configured root"):
        outside.cleanup()
    with pytest.raises(SnapshotError, match="Refusing to remove"):
        root_stage.cleanup()


def test_staged_cleanup_translates_filesystem_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")
    staged = builder.finish(_complete_manifest())

    def reject_remove(_path: Path) -> None:
        raise OSError("synthetic filesystem detail")

    monkeypatch.setattr(snapshot_bundle.shutil, "rmtree", reject_remove)
    with pytest.raises(SnapshotError, match="Unable to remove") as captured:
        staged.cleanup()
    assert "synthetic filesystem detail" not in str(captured.value)


def test_builder_creation_translates_private_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_stage(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("synthetic storage detail")

    monkeypatch.setattr(snapshot_bundle.tempfile, "mkdtemp", reject_stage)
    with pytest.raises(SnapshotError, match="private snapshot staging") as captured:
        SnapshotBundleBuilder(tmp_path / "snapshots")
    assert "synthetic storage detail" not in str(captured.value)


def test_bundle_parameters_and_finish_state_are_strict(tmp_path: Path) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")

    with pytest.raises(ValueError, match="accommodate one UTF-8 code point"):
        builder.add_text(kind="aria", text="value", metadata={}, target_bytes=0)
    with pytest.raises(ValueError, match="sequence must be positive"):
        builder.add_records(kind="dom", sequence=0, records=[], metadata={})
    with pytest.raises(SnapshotError, match="cannot be staged as incomplete"):
        builder.finish({"captureComplete": False})

    staged = builder.finish(_complete_manifest())
    with pytest.raises(SnapshotError, match="already finished"):
        builder.finish(_complete_manifest())
    builder.abort()
    assert staged.staging_dir.exists()
    staged.cleanup()


def test_abort_removes_unfinished_bundle_once(tmp_path: Path) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")
    staging_dir = builder._staging_dir

    builder.abort()
    builder.abort()

    assert not staging_dir.exists()


def test_payload_write_failure_is_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")

    def reject_exclusive(_path: Path) -> Any:
        raise AtomicPersistenceError("synthetic write detail")

    monkeypatch.setattr(
        snapshot_bundle,
        "open_private_binary_exclusive",
        reject_exclusive,
    )
    with pytest.raises(SnapshotError, match="Unable to stage") as captured:
        builder.add_text(kind="aria", text="complete content", metadata={})
    assert "synthetic write detail" not in str(captured.value)


def test_text_chunking_preserves_empty_text_unicode_and_long_lines() -> None:
    assert list(_text_chunks("", 4)) == [""]
    assert list(_text_chunks("abcdef", 2)) == ["ab", "cd", "ef"]
    value = "é\ntext\n"
    chunks = list(_text_chunks(value, TEXT_CHUNK_LIMIT))
    assert "".join(chunks) == value
    assert chunks == ["é\n", "tex", "t\n"]
    assert all(len(chunk.encode("utf-8")) <= TEXT_CHUNK_LIMIT for chunk in chunks)


def test_snapshot_persistence_adapters_delegate_successfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    source = tmp_path / "source"
    destination = tmp_path / "destination"

    monkeypatch.setattr(
        snapshot_persistence.persistence,
        "write_json_atomic",
        lambda path, value: calls.append(("json", (path, value))),
    )
    monkeypatch.setattr(
        snapshot_persistence.persistence,
        "write_bytes_atomic",
        lambda path, value: calls.append(("bytes", (path, value))),
    )
    monkeypatch.setattr(
        snapshot_persistence.persistence,
        "copy_atomic",
        lambda first, second: calls.append(("copy", (first, second))),
    )
    monkeypatch.setattr(
        snapshot_persistence.persistence,
        "digest_file",
        lambda path: hashlib.sha256(str(path).encode()).hexdigest(),
    )

    snapshot_persistence.write_json_atomic(destination, {"complete": True})
    snapshot_persistence.write_bytes_atomic(destination, b"payload")
    snapshot_persistence.copy_atomic(source, destination)
    digest = snapshot_persistence.digest_file(source)

    assert [name for name, _value in calls] == ["json", "bytes", "copy"]
    assert digest == hashlib.sha256(str(source).encode()).hexdigest()


@pytest.mark.parametrize(
    "operation, expected",
    [
        ("write_json_atomic", "Atomic snapshot JSON persistence failed"),
        ("write_bytes_atomic", "Atomic snapshot persistence failed"),
        ("copy_atomic", "Atomic snapshot copy failed"),
        ("digest_file", "Unable to verify snapshot chunk integrity"),
    ],
)
def test_snapshot_persistence_adapters_translate_atomic_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    expected: str,
) -> None:
    def reject(*_args: Any, **_kwargs: Any) -> Any:
        raise AtomicPersistenceError("synthetic persistence detail")

    monkeypatch.setattr(snapshot_persistence.persistence, operation, reject)
    path = tmp_path / "snapshot"
    with pytest.raises(SnapshotError, match=expected) as captured:
        if operation == "write_json_atomic":
            snapshot_persistence.write_json_atomic(path, {})
        elif operation == "write_bytes_atomic":
            snapshot_persistence.write_bytes_atomic(path, b"payload")
        elif operation == "copy_atomic":
            snapshot_persistence.copy_atomic(path, tmp_path / "copy")
        else:
            snapshot_persistence.digest_file(path)
    assert "synthetic persistence detail" not in str(captured.value)
