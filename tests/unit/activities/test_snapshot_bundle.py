"""Complete content-addressed snapshot bundle tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from plantain.activities.snapshot_bundle import SnapshotBundleBuilder
from plantain.activities.ui_errors import SnapshotError

TARGET_TEXT_CHUNK_BYTES = 20


def test_text_chunks_preserve_every_character_and_are_hash_verifiable(
    tmp_path: Path,
) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")
    original = "first line\n" + ("x" * 100) + "\nlast line"

    descriptors = builder.add_text(
        kind="aria",
        text=original,
        metadata={"frameId": "main"},
        target_bytes=TARGET_TEXT_CHUNK_BYTES,
    )
    staged = builder.finish(
        {
            "schemaVersion": "3.0",
            "captureComplete": True,
            "content": {"aria": descriptors},
        }
    )

    payloads = {chunk.relative_file: chunk.path.read_bytes() for chunk in staged.chunks}
    assert (
        b"".join(payloads[str(descriptor["file"])] for descriptor in descriptors).decode("utf-8")
        == original
    )
    for chunk in staged.chunks:
        assert hashlib.sha256(chunk.path.read_bytes()).hexdigest() == chunk.sha256
        assert chunk.relative_file.startswith(f"chunks/{chunk.sha256[:2]}/")
    staged.cleanup()
    assert not staged.staging_dir.exists()


def test_record_batches_have_no_total_count_limit_and_deduplicate_payloads(
    tmp_path: Path,
) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")
    records = [{"index": index, "role": "button"} for index in range(2_001)]

    first = builder.add_records(
        kind="dom",
        sequence=1,
        records=records,
        metadata={"frameId": "main"},
    )
    second = builder.add_records(
        kind="dom",
        sequence=2,
        records=records,
        metadata={"frameId": "main"},
    )
    staged = builder.finish(
        {
            "schemaVersion": "3.0",
            "captureComplete": True,
            "content": {"dom": [first, second]},
        }
    )

    assert first["recordCount"] == len(records)
    assert first["file"] == second["file"]
    assert len(staged.chunks) == 1
    staged.cleanup()


def test_oversized_utf8_line_is_split_without_loss(tmp_path: Path) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")
    original = ("wide-🙂" * 30) + "\n"

    descriptors = builder.add_text(
        kind="aria",
        text=original,
        metadata={"frameId": "main"},
        target_bytes=TARGET_TEXT_CHUNK_BYTES,
    )
    staged = builder.finish({"captureComplete": True, "content": descriptors})

    assert all(chunk.size_bytes <= TARGET_TEXT_CHUNK_BYTES for chunk in staged.chunks)
    payloads = {chunk.relative_file: chunk.path.read_bytes() for chunk in staged.chunks}
    assert (
        b"".join(payloads[str(descriptor["file"])] for descriptor in descriptors).decode()
        == original
    )
    staged.cleanup()


def test_capture_storage_envelope_fails_and_private_stage_aborts(tmp_path: Path) -> None:
    builder = SnapshotBundleBuilder(
        tmp_path / "snapshots",
        max_capture_bytes=80,
    )

    with pytest.raises(SnapshotError, match="SNAPSHOT_MAX_CAPTURE_BYTES"):
        builder.add_text(
            kind="aria",
            text="x" * 100,
            metadata={"frameId": "main"},
            target_bytes=20,
        )

    staging_dir = builder._staging_dir
    builder.abort()
    assert not staging_dir.exists()


def test_record_batch_respects_working_set_envelope(tmp_path: Path) -> None:
    builder = SnapshotBundleBuilder(tmp_path / "snapshots")

    with pytest.raises(SnapshotError, match="SNAPSHOT_WORKING_SET_BYTES"):
        builder.add_records(
            kind="dom",
            sequence=1,
            records=[{"text": "x" * 256}],
            metadata={"frameId": "main"},
            max_payload_bytes=128,
        )

    builder.abort()
