"""Transactional complete snapshot registry tests."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from plantain.activities.snapshot_bundle import SnapshotBundleBuilder, StagedSnapshot
from plantain.activities.snapshot_registry_complete import (
    CompleteSnapshotRegistry,
    PendingSnapshot,
)
from plantain.activities.ui_errors import SnapshotError
from plantain.persistence import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE


def _summary(*, buttons: int) -> dict[str, Any]:
    return {
        "normalizedKey": "example.test::/inventory",
        "normalizedFrameUrl": "about:blank",
        "elementCounts": {
            "formFields": 0,
            "buttons": buttons,
            "selects": 0,
            "links": 0,
        },
        "keyIds": [f"button-{index}" for index in range(buttons)],
        "nestedElementCounts": [],
    }


def _capture(
    snapshots_dir: Path,
    *,
    title: str = "Inventory Page",
    url: str = "https://example.test/inventory",
    buttons: int = 1,
    content: str = '- button "Checkout"',
) -> StagedSnapshot:
    builder = SnapshotBundleBuilder(snapshots_dir)
    dom = builder.add_text(
        kind="dom",
        text=content,
        metadata={"frameId": "main"},
    )
    return builder.finish(
        {
            "schemaVersion": "3.0",
            "captureComplete": True,
            "url": url,
            "pageTitle": title,
            "content": {
                "aria": {
                    "format": "semantic-accessibility-records-v1",
                    "source": "dom",
                    "depth": "unlimited",
                    "recordCount": 1,
                },
                "dom": {
                    "format": "semantic-records-v1",
                    "recordCount": 1,
                    "chunkCount": len(dom),
                    "chunks": dom,
                },
                "network": {
                    "format": "lifecycle-events-v1",
                    "recordCount": 0,
                    "chunkCount": 0,
                    "chunks": [],
                },
            },
            "structuralSummary": _summary(buttons=buttons),
        }
    )


def test_creates_page_title_manifest_and_materializes_verified_chunks(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    staged = _capture(snapshots_dir)
    expected_chunk = staged.chunks[0].relative_file
    registry = CompleteSnapshotRegistry(snapshots_dir)

    result = registry.register_batch(
        [PendingSnapshot(staged=staged, activity="checkout_inventory")]
    )

    assert result[0].canonical_file == "inventory_page.semantic.json"
    assert result[0].status == "created"
    assert result[0].evidence_state == "verified"
    assert result[0].failure_stage is None
    assert not staged.staging_dir.exists()
    assert (snapshots_dir / expected_chunk).is_file()
    manifest = json.loads((snapshots_dir / result[0].canonical_file).read_text("utf-8"))
    assert manifest["captureComplete"] is True
    assert manifest["evidenceState"] == "verified"
    registry_path = snapshots_dir / "registry.json"
    if os.name != "nt":
        registry_path.chmod(0o666)
        assert registry.resolve_activity("checkout_inventory") == (
            snapshots_dir / result[0].canonical_file
        )
        assert stat.S_IMODE(registry_path.stat().st_mode) == PRIVATE_FILE_MODE
    document = json.loads(registry_path.read_text("utf-8"))
    assert document["entries"][0]["activities"] == ["checkout_inventory"]
    assert document["entries"][0]["diagnostics"] == []
    assert document["entries"][0]["evidenceState"] == "verified"
    if os.name != "nt":
        directories = (
            snapshots_dir,
            snapshots_dir / ".staging",
            snapshots_dir / "history",
            (snapshots_dir / expected_chunk).parent,
        )
        files = (
            snapshots_dir / ".registry.lock",
            snapshots_dir / "registry.json",
            snapshots_dir / result[0].canonical_file,
            snapshots_dir / expected_chunk,
        )
        assert all(
            stat.S_IMODE(path.stat().st_mode) == PRIVATE_DIRECTORY_MODE for path in directories
        )
        assert all(stat.S_IMODE(path.stat().st_mode) == PRIVATE_FILE_MODE for path in files)


def test_verified_evidence_reader_retains_only_bounded_dom_content(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    content = '- button "Checkout"\n- textbox "Name"'
    registry = CompleteSnapshotRegistry(snapshots_dir)
    created = registry.register_batch(
        [PendingSnapshot(_capture(snapshots_dir, content=content), "inventory")]
    )[0]

    summaries = registry.verified_summaries()

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.canonical_file == created.canonical_file
    assert summary.activities == ("inventory",)
    assert summary.normalized_key == "example.test::/inventory"
    assert ("buttons", 1) in summary.element_counts
    assert summary.key_ids == ("button-0",)
    assert summary.summary_limited is False

    byte_limit = len(b'- button "Checkout"\n')
    excerpt = registry.read_verified_excerpt(
        created.canonical_file,
        max_bytes=byte_limit,
    )

    assert excerpt.summary == summary
    assert excerpt.content == content[:byte_limit]
    assert excerpt.truncated is True
    with pytest.raises(ValueError, match="outside"):
        registry.read_verified_excerpt(created.canonical_file, max_bytes=0)


def test_diagnostic_reader_requires_exact_failure_identity(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    content = '- button "Retry"\n- textbox "Name"'
    registry = CompleteSnapshotRegistry(snapshots_dir)
    created = registry.register_batch(
        [
            PendingSnapshot(
                _capture(snapshots_dir, content=content),
                "checkout",
                evidence_state="diagnostic",
                failure_stage="action",
            )
        ]
    )[0]

    excerpt = registry.read_diagnostic_excerpt(
        created.canonical_file,
        "checkout",
        "action",
        max_bytes=len(content.encode()),
    )

    assert excerpt.canonical_file == created.canonical_file
    assert excerpt.activity == "checkout"
    assert excerpt.failure_stage == "action"
    assert excerpt.content == content
    with pytest.raises(SnapshotError, match="exact diagnostic"):
        registry.read_diagnostic_excerpt(
            created.canonical_file,
            "other",
            "action",
            max_bytes=1,
        )
    with pytest.raises(SnapshotError, match="exact diagnostic"):
        registry.read_diagnostic_excerpt(
            created.canonical_file,
            "checkout",
            "verification",
            max_bytes=1,
        )


def test_duplicate_reuses_baseline_without_materializing_discarded_content(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    first = _capture(snapshots_dir, content="first capture")
    registry.register_batch([PendingSnapshot(first, "inventory_primary")])
    second = _capture(snapshots_dir, content="discarded duplicate payload")
    discarded_chunk = second.chunks[0].relative_file

    result = registry.register_batch([PendingSnapshot(second, "inventory_alias")])

    assert result[0].status == "reused"
    assert not (snapshots_dir / discarded_chunk).exists()
    assert not second.staging_dir.exists()
    document = json.loads((snapshots_dir / "registry.json").read_text("utf-8"))
    assert document["entries"][0]["activities"] == [
        "inventory_alias",
        "inventory_primary",
    ]


def test_diagnostic_capture_preserves_its_actual_bundle(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    verified = _capture(snapshots_dir, content="verified capture")
    registry.register_batch([PendingSnapshot(verified, "inventory")])
    diagnostic = _capture(snapshots_dir, content="failed attempt actual DOM")
    expected_chunk = diagnostic.chunks[0].relative_file

    result = registry.register_batch(
        [
            PendingSnapshot(
                diagnostic,
                "failed_inventory",
                evidence_state="diagnostic",
                failure_stage="action",
            )
        ]
    )[0]

    assert result.evidence_state == "diagnostic"
    assert result.failure_stage == "action"
    assert (snapshots_dir / expected_chunk).is_file()
    manifest = json.loads((snapshots_dir / result.canonical_file).read_text("utf-8"))
    assert manifest["evidenceState"] == "diagnostic"
    assert manifest["failureStage"] == "action"
    document = json.loads((snapshots_dir / "registry.json").read_text("utf-8"))
    entry = next(
        item for item in document["entries"] if item["canonicalFile"] == result.canonical_file
    )
    assert entry["activities"] == []
    assert entry["evidenceState"] == "diagnostic"
    assert entry["diagnostics"][0]["activity"] == "failed_inventory"
    assert entry["diagnostics"][0]["failureStage"] == "action"
    assert registry.resolve_filename(result.canonical_file) == (
        snapshots_dir / result.canonical_file
    )
    with pytest.raises(SnapshotError, match="No snapshot found"):
        registry.resolve_activity("failed_inventory")
    with pytest.raises(SnapshotError, match="verification diagnostic evidence"):
        registry.promote_diagnostic(result.canonical_file, "failed_inventory")


def test_verification_diagnostic_promotes_after_assertions_pass(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    staged = _capture(snapshots_dir, content="assertion candidate")
    diagnostic = registry.register_batch(
        [
            PendingSnapshot(
                staged,
                "inventory",
                evidence_state="diagnostic",
                failure_stage="verification",
            )
        ]
    )[0]

    promoted = registry.promote_diagnostic(diagnostic.canonical_file, "inventory")

    assert promoted.evidence_state == "verified"
    assert promoted.failure_stage is None
    assert registry.resolve_activity("inventory") == (snapshots_dir / diagnostic.canonical_file)
    document = json.loads((snapshots_dir / "registry.json").read_text("utf-8"))
    assert document["entries"][0]["activities"] == ["inventory"]
    assert document["entries"][0]["diagnostics"] == []
    assert document["entries"][0]["evidenceState"] == "verified"
    manifest = json.loads((snapshots_dir / diagnostic.canonical_file).read_text("utf-8"))
    assert manifest["evidenceState"] == "verified"
    assert "failureStage" not in manifest


def test_batch_compares_only_frozen_baseline(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    first = _capture(snapshots_dir, buttons=1, content="one button")
    second = _capture(snapshots_dir, buttons=2, content="two buttons")

    result = CompleteSnapshotRegistry(snapshots_dir).register_batch(
        [PendingSnapshot(first, "state_one"), PendingSnapshot(second, "state_two")]
    )

    assert [item.status for item in result] == ["created", "created"]
    assert result[0].canonical_file == "inventory_page.semantic.json"
    assert result[1].canonical_file.startswith("inventory_page_state_")


def test_rejects_tampered_stage_and_removes_private_capture(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    staged = _capture(snapshots_dir)
    staged.chunks[0].path.write_bytes(b"tampered")

    with pytest.raises(SnapshotError, match="integrity verification"):
        CompleteSnapshotRegistry(snapshots_dir).register_batch(
            [PendingSnapshot(staged, "inventory")]
        )

    assert not staged.staging_dir.exists()
    assert not (snapshots_dir / "inventory_page.semantic.json").exists()


def test_registry_commit_failure_rolls_back_canonical_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    initial = _capture(snapshots_dir, buttons=1, content="initial")
    created = registry.register_batch([PendingSnapshot(initial, "inventory")])[0]
    manifest_path = snapshots_dir / created.canonical_file
    registry_path = snapshots_dir / "registry.json"
    original_manifest = manifest_path.read_bytes()
    original_registry = registry_path.read_bytes()
    original_files = {
        path.relative_to(snapshots_dir).as_posix(): path.read_bytes()
        for path in snapshots_dir.rglob("*")
        if path.is_file()
    }
    replacement = _capture(snapshots_dir, buttons=2, content="replacement")

    def fail_registry_write(_value: Any) -> None:
        raise SnapshotError("simulated registry commit failure")

    monkeypatch.setattr(registry, "_write_registry", fail_registry_write)
    with pytest.raises(SnapshotError, match="simulated registry commit failure"):
        registry.register_batch([PendingSnapshot(replacement, "inventory")])

    assert manifest_path.read_bytes() == original_manifest
    assert registry_path.read_bytes() == original_registry
    assert {
        path.relative_to(snapshots_dir).as_posix(): path.read_bytes()
        for path in snapshots_dir.rglob("*")
        if path.is_file()
    } == original_files
    assert not replacement.staging_dir.exists()
