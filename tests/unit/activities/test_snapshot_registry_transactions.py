"""Complete isolated transaction and security coverage for the snapshot registry."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from plantain.activities import snapshot_persistence, snapshot_registry_complete
from plantain.activities.snapshot_bundle import SnapshotBundleBuilder, StagedSnapshot
from plantain.activities.snapshot_registry_complete import (
    REGISTRY_SCHEMA_VERSION,
    CompleteSnapshotRegistry,
    PendingSnapshot,
    SnapshotRegistrationCancellation,
    SnapshotRegistrationCancellationError,
    _add_activity,
    _describe_change,
    _entry_summary,
    _MutationJournal,
    _normalized_nested,
    _required_mapping,
    _required_string,
    _same_structure,
    _slug,
    _working_entry,
)
from plantain.activities.ui_errors import SnapshotError

SAME_TIMESTAMP_HISTORY_COUNT = 2


def _summary(
    *,
    buttons: int = 1,
    normalized_key: str = "example.test::/inventory",
    nested: list[dict[str, Any]] | None = None,
    frame_url: str = "about:blank",
) -> dict[str, Any]:
    return {
        "normalizedKey": normalized_key,
        "normalizedFrameUrl": frame_url,
        "elementCounts": {
            "formFields": 0,
            "buttons": buttons,
            "selects": 0,
            "links": 0,
        },
        "keyIds": [f"button-{index}" for index in range(buttons)],
        "nestedElementCounts": nested or [],
    }


def _file_state(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _capture(
    snapshots_dir: Path,
    *,
    title: str = "Inventory Page",
    url: str = "https://example.test/inventory",
    activity_content: str = "- button Checkout",
    summary: dict[str, Any] | None = None,
) -> StagedSnapshot:
    builder = SnapshotBundleBuilder(snapshots_dir)
    dom = builder.add_text(
        kind="dom",
        text=activity_content,
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
            "structuralSummary": summary or _summary(),
        }
    )


def _verification_diagnostic(
    registry: CompleteSnapshotRegistry,
    snapshots_dir: Path,
) -> Any:
    return registry.register_batch(
        [
            PendingSnapshot(
                _capture(snapshots_dir),
                "inventory",
                evidence_state="diagnostic",
                failure_stage="verification",
            )
        ]
    )[0]


def test_empty_batch_and_activity_resolution_contract(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)

    assert registry.register_batch([]) == []
    with pytest.raises(SnapshotError, match="No snapshot found"):
        registry.resolve_activity("missing")

    staged = _capture(snapshots_dir)
    created = registry.register_batch([PendingSnapshot(staged, "inventory")])[0]
    assert registry.resolve_activity("inventory") == snapshots_dir / created.canonical_file
    assert registry.resolve_filename(created.canonical_file) == (
        snapshots_dir / created.canonical_file
    )

    document = json.loads((snapshots_dir / "registry.json").read_text(encoding="utf-8"))
    document["entries"][0]["deprecated"] = ["legacy_inventory.semantic.json"]
    (snapshots_dir / "registry.json").write_text(json.dumps(document), encoding="utf-8")
    assert registry.resolve_filename("legacy_inventory.semantic.json") == (
        snapshots_dir / created.canonical_file
    )
    with pytest.raises(SnapshotError, match="is not registered"):
        registry.resolve_filename("unknown.semantic.json")


def test_activity_resolution_rejects_multiple_dom_states(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    first = _capture(snapshots_dir, summary=_summary(buttons=1))
    second = _capture(snapshots_dir, summary=_summary(buttons=2))
    registry.register_batch(
        [PendingSnapshot(first, "inventory"), PendingSnapshot(second, "inventory")]
    )

    with pytest.raises(SnapshotError, match="multiple DOM states"):
        registry.resolve_activity("inventory")


def test_resolution_verifies_manifest_and_chunk_integrity(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    created = registry.register_batch([PendingSnapshot(_capture(snapshots_dir), "inventory")])[0]
    manifest_path = snapshots_dir / created.canonical_file
    original_manifest = manifest_path.read_bytes()
    manifest = json.loads(original_manifest)
    manifest["schemaVersion"] = "2.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotError, match="unsupported schema version"):
        registry.resolve_activity("inventory")

    manifest_path.write_bytes(original_manifest)
    descriptor = manifest["content"]["dom"]["chunks"][0]
    chunk_path = snapshots_dir / descriptor["file"]
    chunk_path.write_bytes(b"corrupt")
    with pytest.raises(SnapshotError, match="failed integrity verification"):
        registry.resolve_filename(created.canonical_file)


def test_resolution_rejects_symlinked_manifest_and_chunk(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    created = registry.register_batch([PendingSnapshot(_capture(snapshots_dir), "inventory")])[0]
    manifest_path = snapshots_dir / created.canonical_file
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    outside_manifest = tmp_path / "outside.semantic.json"
    outside_manifest.write_bytes(manifest_bytes)
    manifest_path.unlink()
    manifest_path.symlink_to(outside_manifest)
    with pytest.raises(SnapshotError, match="unreadable or unsafe"):
        registry.resolve_activity("inventory")

    manifest_path.unlink()
    manifest_path.write_bytes(manifest_bytes)
    chunk_path = snapshots_dir / manifest["content"]["dom"]["chunks"][0]["file"]
    outside_chunk = tmp_path / "outside.txt"
    outside_chunk.write_bytes(chunk_path.read_bytes())
    chunk_path.unlink()
    chunk_path.symlink_to(outside_chunk)
    with pytest.raises(SnapshotError, match="unreadable or unsafe"):
        registry.resolve_activity("inventory")


def test_cross_build_replaces_manifest_and_preserves_deprecation_history(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    old = _capture(
        snapshots_dir,
        title="Old Inventory",
        url="https://example.test/portalvOLD/inventory",
    )
    old_result = registry.register_batch([PendingSnapshot(old, "old_activity")])[0]
    current = _capture(
        snapshots_dir,
        title="Current Inventory",
        url="https://example.test/portalvNEW/inventory",
        activity_content="current content",
    )

    result = registry.register_batch([PendingSnapshot(current, "current_activity")])[0]

    assert result.status == "cross_build_updated"
    assert result.canonical_file != old_result.canonical_file
    assert not (snapshots_dir / old_result.canonical_file).exists()
    assert (snapshots_dir / result.canonical_file).is_file()
    assert list((snapshots_dir / "history").glob("*.semantic.json"))
    document = json.loads((snapshots_dir / "registry.json").read_text(encoding="utf-8"))
    entry = document["entries"][0]
    assert entry["activities"] == ["current_activity", "old_activity"]
    assert old_result.canonical_file in entry["deprecated"]


def test_obsolete_deletion_failure_restores_the_complete_old_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    old = _capture(snapshots_dir, title="Old", url="https://example.test/portalvOLD/page")
    old_result = registry.register_batch([PendingSnapshot(old, "old_activity")])[0]
    before = _file_state(snapshots_dir)
    current = _capture(
        snapshots_dir,
        title="Current",
        url="https://example.test/portalvNEW/page",
        activity_content="new content",
    )
    durable_unlink = snapshot_persistence.unlink_durable

    def reject_old(path: Path, *, missing_ok: bool = True) -> None:
        if path == snapshots_dir / old_result.canonical_file and not missing_ok:
            raise SnapshotError("synthetic obsolete deletion failure")
        durable_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(snapshot_persistence, "unlink_durable", reject_old)
    with pytest.raises(SnapshotError, match="synthetic obsolete deletion failure"):
        registry.register_batch([PendingSnapshot(current, "current_activity")])

    assert _file_state(snapshots_dir) == before
    assert registry.resolve_activity("old_activity").name == old_result.canonical_file
    assert not current.staging_dir.exists()


def test_existing_activity_structural_change_updates_in_place(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    initial = _capture(snapshots_dir, summary=_summary(buttons=1))
    original = registry.register_batch([PendingSnapshot(initial, "inventory")])[0]
    replacement = _capture(
        snapshots_dir,
        summary=_summary(buttons=2),
        activity_content="two buttons",
    )

    updated = registry.register_batch([PendingSnapshot(replacement, "inventory")])[0]

    assert updated.status == "updated"
    assert updated.canonical_file == original.canonical_file
    document = json.loads((snapshots_dir / "registry.json").read_text(encoding="utf-8"))
    assert "elementCounts" in document["entries"][0]["changeNote"]
    assert list((snapshots_dir / "history").glob("*.semantic.json"))


def test_history_allocation_preserves_same_timestamp_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    first = CompleteSnapshotRegistry(snapshots_dir)
    second = CompleteSnapshotRegistry(snapshots_dir)
    monkeypatch.setattr(
        snapshot_registry_complete,
        "_now",
        lambda: "2026-09-02T12:00:00+00:00",
    )
    first.register_batch([PendingSnapshot(_capture(snapshots_dir), "inventory")])
    second.register_batch(
        [
            PendingSnapshot(
                _capture(
                    snapshots_dir,
                    activity_content="two buttons",
                    summary=_summary(buttons=2),
                ),
                "inventory",
            )
        ]
    )
    first.register_batch(
        [
            PendingSnapshot(
                _capture(
                    snapshots_dir,
                    activity_content="three buttons",
                    summary=_summary(buttons=3),
                ),
                "inventory",
            )
        ]
    )

    history = sorted((snapshots_dir / "history").glob("*.semantic.json"))
    assert len(history) == SAME_TIMESTAMP_HISTORY_COUNT
    assert history[0].name != history[1].name


def test_existing_activity_with_multiple_candidates_requires_distinct_name(
    tmp_path: Path,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    first = _capture(snapshots_dir, summary=_summary(buttons=1))
    second = _capture(snapshots_dir, summary=_summary(buttons=2))
    registry.register_batch(
        [PendingSnapshot(first, "inventory"), PendingSnapshot(second, "inventory")]
    )
    third = _capture(snapshots_dir, summary=_summary(buttons=3))

    with pytest.raises(SnapshotError, match="multiple canonical states"):
        registry.register_batch([PendingSnapshot(third, "inventory")])
    assert not third.staging_dir.exists()


def test_mutation_journal_restores_original_and_removes_created_file(tmp_path: Path) -> None:
    existing = tmp_path / "existing.json"
    created = tmp_path / "created.json"
    existing.write_bytes(b"original")
    journal = _MutationJournal()
    journal.remember(existing)
    journal.remember(existing)
    journal.remember(created)
    existing.write_bytes(b"changed")
    created.write_bytes(b"created")

    journal.rollback()

    assert existing.read_bytes() == b"original"
    assert not created.exists()


def test_mutation_journal_translates_read_and_rollback_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "manifest.json"
    target.write_bytes(b"original")
    original_read = snapshot_registry_complete._read_private_bytes

    def reject_read(path: Path, *, label: str) -> bytes:
        if path == target:
            raise OSError("synthetic read detail")
        return original_read(path, label=label)

    monkeypatch.setattr(snapshot_registry_complete, "_read_private_bytes", reject_read)
    with pytest.raises(SnapshotError, match="Unable to journal") as captured:
        _MutationJournal().remember(target)
    assert "synthetic read detail" not in str(captured.value)
    monkeypatch.undo()

    journal = _MutationJournal(originals={target: b"original"})

    def reject_restore(_path: Path, _payload: bytes) -> None:
        raise SnapshotError("synthetic restore detail")

    monkeypatch.setattr(snapshot_persistence, "write_bytes_atomic", reject_restore)
    with pytest.raises(SnapshotError, match="Unable to roll back"):
        journal.rollback()


class _CleanupStage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True
        if self.fail:
            raise SnapshotError("synthetic cleanup detail")


def test_registration_combines_operation_and_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CompleteSnapshotRegistry(tmp_path / "snapshots")
    stage = _CleanupStage(fail=True)

    def reject_registration(_items: Any, _cancellation: Any) -> Any:
        raise SnapshotError("synthetic operation detail")

    monkeypatch.setattr(registry, "_register_locked", reject_registration)
    pending = PendingSnapshot(cast("StagedSnapshot", stage), "inventory")
    with pytest.raises(SnapshotError, match="registration or staging cleanup") as captured:
        registry.register_batch([pending])
    assert stage.cleaned is True
    assert captured.value.__cause__ is not None


def test_registration_reraises_operation_only_and_rejects_missing_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CompleteSnapshotRegistry(tmp_path / "snapshots")
    stage = _CleanupStage()

    def reject_registration(_items: Any, _cancellation: Any) -> Any:
        raise SnapshotError("operation failed")

    monkeypatch.setattr(registry, "_register_locked", reject_registration)
    with pytest.raises(SnapshotError, match="operation failed"):
        registry.register_batch([PendingSnapshot(cast("StagedSnapshot", stage), "inventory")])
    assert stage.cleaned is True

    monkeypatch.setattr(
        registry,
        "_register_locked",
        lambda _items, _cancellation: None,
    )
    with pytest.raises(SnapshotError, match="produced no result"):
        registry.register_batch(
            [PendingSnapshot(cast("StagedSnapshot", _CleanupStage()), "inventory")]
        )


def test_cancellation_during_commit_rolls_back_canonical_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    staged = _capture(snapshots_dir)
    cancellation = SnapshotRegistrationCancellation()
    write_registry = registry._write_registry

    def cancel_after_write(value: Any) -> None:
        write_registry(value)
        cancellation.cancel()

    monkeypatch.setattr(registry, "_write_registry", cancel_after_write)
    with pytest.raises(SnapshotRegistrationCancellationError, match="cancelled before commit"):
        registry.register_batch(
            [PendingSnapshot(staged, "inventory")],
            cancellation=cancellation,
        )

    assert not (snapshots_dir / "registry.json").exists()
    assert list(snapshots_dir.glob("*.semantic.json")) == []
    assert _file_state(snapshots_dir) == {".registry.lock": b""}
    assert not staged.staging_dir.exists()


def test_cancellation_during_promotion_restores_diagnostic_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    diagnostic = _verification_diagnostic(registry, snapshots_dir)
    before = _file_state(snapshots_dir)
    cancellation = SnapshotRegistrationCancellation()
    write_registry = registry._write_registry

    def cancel_after_write(value: Any) -> None:
        write_registry(value)
        cancellation.cancel()

    monkeypatch.setattr(registry, "_write_registry", cancel_after_write)
    with pytest.raises(SnapshotRegistrationCancellationError, match="cancelled before commit"):
        registry.promote_diagnostic(
            diagnostic.canonical_file,
            "inventory",
            cancellation=cancellation,
        )
    assert _file_state(snapshots_dir) == before
    with pytest.raises(SnapshotError, match="No snapshot found"):
        registry.resolve_activity("inventory")


def test_promotion_write_failure_restores_complete_diagnostic_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    diagnostic = _verification_diagnostic(registry, snapshots_dir)
    before = _file_state(snapshots_dir)

    def reject_registry_write(_value: Any) -> None:
        raise SnapshotError("synthetic promotion failure")

    monkeypatch.setattr(registry, "_write_registry", reject_registry_write)
    with pytest.raises(SnapshotError, match="synthetic promotion failure"):
        registry.promote_diagnostic(diagnostic.canonical_file, "inventory")
    assert _file_state(snapshots_dir) == before
    with pytest.raises(SnapshotError, match="No snapshot found"):
        registry.resolve_activity("inventory")


def test_materialize_chunk_validates_path_existing_and_copied_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    staged = _capture(snapshots_dir)
    chunk = staged.chunks[0]
    journal = _MutationJournal()

    with pytest.raises(SnapshotError, match="invalid content-addressed path"):
        registry._materialize_chunk(
            replace(chunk, relative_file="chunks/invalid.txt"),
            journal,
        )

    destination = snapshots_dir / chunk.relative_file
    destination.parent.mkdir(parents=True)
    destination.write_bytes(chunk.path.read_bytes())
    registry._materialize_chunk(chunk, journal)
    destination.write_bytes(b"corrupt")
    with pytest.raises(SnapshotError, match="Existing content-addressed"):
        registry._materialize_chunk(chunk, journal)

    destination.unlink()

    def corrupt_copy(_source: Path, target: Path) -> None:
        target.write_bytes(b"corrupt")

    monkeypatch.setattr(snapshot_persistence, "copy_atomic", corrupt_copy)
    with pytest.raises(SnapshotError, match="failed integrity verification"):
        registry._materialize_chunk(chunk, journal)
    journal.rollback()
    assert not destination.exists()
    staged.cleanup()


def test_staged_validation_rejects_incomplete_or_malformed_manifest(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    cases: list[tuple[str, Any]] = [
        (
            "unsupported schema version",
            lambda value: value.update(schemaVersion="2.0"),
        ),
        ("Incomplete snapshots", lambda value: value.update(captureComplete=False)),
        ("requires a 'content' mapping", lambda value: value.update(content=[])),
        (
            "DOM-sourced accessibility",
            lambda value: value["content"].update(aria=[]),
        ),
        (
            "DOM source contract",
            lambda value: value["content"].update(aria={"chunks": []}),
        ),
        (
            "DOM source contract",
            lambda value: value["content"]["aria"].update(format="legacy"),
        ),
        (
            "DOM source contract",
            lambda value: value["content"]["aria"].update(source="network"),
        ),
        (
            "DOM source contract",
            lambda value: value["content"]["aria"].update(depth="bounded"),
        ),
        (
            "non-negative integers",
            lambda value: value["content"]["aria"].update(recordCount=True),
        ),
        (
            "non-negative integers",
            lambda value: value["content"]["dom"].update(recordCount=-1),
        ),
        (
            "record counts do not match",
            lambda value: value["content"]["aria"].update(recordCount=2),
        ),
        (
            "invalid chunk descriptor",
            lambda value: value["content"]["dom"].update(chunks=[7]),
        ),
        (
            "descriptor requires a file",
            lambda value: value["content"]["dom"].update(chunks=[{}]),
        ),
        (
            "invalid dom chunk count",
            lambda value: value["content"]["dom"].update(chunks=[]),
        ),
        (
            "do not match",
            lambda value: value["content"]["dom"].update(chunks=[], chunkCount=0),
        ),
        (
            "do not match",
            lambda value: value["content"]["dom"]["chunks"][0].update(byteCount=999),
        ),
    ]

    for message, mutate in cases:
        staged = _capture(snapshots_dir)
        mutate(staged.manifest)
        with pytest.raises(SnapshotError, match=message):
            registry._validate_staged(staged)
        staged.cleanup()


def test_staged_validation_rejects_escape_and_symbolic_link(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    registry = CompleteSnapshotRegistry(snapshots_dir)
    staged = _capture(snapshots_dir)
    chunk = staged.chunks[0]
    outside = tmp_path / "outside.txt"
    outside.write_bytes(chunk.path.read_bytes())
    staged.chunks = (replace(chunk, path=outside),)
    with pytest.raises(SnapshotError, match="escapes its private staging"):
        registry._validate_staged(staged)
    staged.cleanup()

    linked = _capture(snapshots_dir)
    linked_chunk = linked.chunks[0]
    real_path = linked_chunk.path.with_name("real.txt")
    linked_chunk.path.rename(real_path)
    linked_chunk.path.symlink_to(real_path.name)
    with pytest.raises(SnapshotError, match="symbolic links"):
        registry._validate_staged(linked)
    linked.cleanup()


def test_archive_filename_and_registry_input_guards(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    registry = CompleteSnapshotRegistry(snapshots_dir)

    with pytest.raises(SnapshotError, match="missing from disk"):
        registry._archive(
            {"canonicalFile": "missing.semantic.json", "lastUpdated": "2026-01-01"},
            _MutationJournal(),
        )
    for filename in ("../escape.semantic.json", "snapshot.json", "nested/page.semantic.json"):
        with pytest.raises(SnapshotError, match="filename is unsafe"):
            registry._manifest_path(filename)

    assert registry._read_registry() == {
        "schemaVersion": REGISTRY_SCHEMA_VERSION,
        "entries": [],
        "lastUpdated": None,
    }
    registry._registry_path.write_text("[]", encoding="utf-8")
    with pytest.raises(SnapshotError, match="entries array"):
        registry._read_registry()
    registry._registry_path.write_text(
        json.dumps({"schemaVersion": "3.0", "entries": []}),
        encoding="utf-8",
    )
    with pytest.raises(SnapshotError, match="unsupported schema version"):
        registry._read_registry()
    registry._registry_path.write_text("invalid", encoding="utf-8")
    with pytest.raises(SnapshotError, match="invalid JSON"):
        registry._read_registry()
    registry._registry_path.write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotError, match="entries array"):
        registry._read_registry()


def test_pending_snapshot_requires_consistent_diagnostic_state() -> None:
    staged = cast("StagedSnapshot", object())

    with pytest.raises(ValueError, match="exactly one failure stage"):
        PendingSnapshot(staged, "inventory", failure_stage="action")
    with pytest.raises(ValueError, match="exactly one failure stage"):
        PendingSnapshot(staged, "inventory", evidence_state="diagnostic")

    pending = PendingSnapshot(
        staged,
        "inventory",
        evidence_state="diagnostic",
        failure_stage="verification",
    )
    assert pending.evidence_state == "diagnostic"
    assert pending.failure_stage == "verification"


def test_new_filename_handles_slug_and_state_collisions(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    registry = CompleteSnapshotRegistry(snapshots_dir)
    summary = _summary()

    assert registry._new_filename("!!!", summary=summary, reserved=set()) == (
        "untitled_page.semantic.json"
    )
    base = snapshots_dir / "inventory.semantic.json"
    base.write_text("existing", encoding="utf-8")
    state = registry._new_filename("Inventory", summary=summary, reserved=set())
    (snapshots_dir / state).write_text("existing state", encoding="utf-8")
    suffixed = registry._new_filename("Inventory", summary=summary, reserved=set())
    assert suffixed.endswith("_2.semantic.json")


def test_structure_and_registry_helpers_cover_nested_and_invalid_shapes() -> None:
    flat = _summary(buttons=1)
    entry = {"structuralSummary": flat}
    assert _same_structure(flat, entry) is True
    assert _same_structure(_summary(buttons=2), entry) is False
    nested = [
        {
            "frameId": "main.0",
            "parentFrameId": "main",
            "normalizedFrameUrl": "example.test::/frame",
            "elementCounts": {"buttons": 1},
            "nestedKeyIds": ["submit"],
        }
    ]
    nested_summary = _summary(nested=nested, frame_url="example.test::/frame")
    assert _same_structure(nested_summary, {"structuralSummary": nested_summary}) is True
    assert (
        _same_structure(
            _summary(nested=nested, frame_url="example.test::/other"),
            {"structuralSummary": nested_summary},
        )
        is False
    )
    changed_main = _summary(
        buttons=2,
        nested=nested,
        frame_url="example.test::/frame",
    )
    assert _same_structure(changed_main, {"structuralSummary": nested_summary}) is False
    changed_parent = [dict(nested[0], parentFrameId="main.1")]
    assert (
        _same_structure(
            _summary(nested=changed_parent, frame_url="example.test::/frame"),
            {"structuralSummary": nested_summary},
        )
        is False
    )
    reordered = [nested[0], dict(nested[0], frameId="main.1")]
    reversed_summary = _summary(
        nested=list(reversed(reordered)),
        frame_url="example.test::/frame",
    )
    ordered_summary = _summary(nested=reordered, frame_url="example.test::/frame")
    assert _same_structure(reversed_summary, {"structuralSummary": ordered_summary}) is False
    assert _same_structure(nested_summary, entry) is False
    assert _normalized_nested("invalid") == []
    assert _normalized_nested(["ignored"]) == []

    assert _describe_change(flat, flat) == "Structural fields changed: unknown"
    assert _entry_summary({"structuralSummary": []}) == {}
    entries = [{"canonicalFile": "page.semantic.json", "activities": ["second"]}]
    assert _working_entry(entries, "page.semantic.json") is entries[0]
    _add_activity(entries[0], "first")
    assert entries[0]["activities"] == ["first", "second"]
    with pytest.raises(SnapshotError, match="disappeared during update"):
        _working_entry(entries, "missing.semantic.json")
    with pytest.raises(SnapshotError, match="non-empty 'url'"):
        _required_string({}, "url")
    with pytest.raises(SnapshotError, match=r"structuralSummary.*mapping"):
        _required_mapping({}, "structuralSummary")
    assert _slug(" Login — Swag Labs ") == "login_swag_labs"
