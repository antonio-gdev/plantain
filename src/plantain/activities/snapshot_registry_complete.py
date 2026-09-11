"""Transactional registry for complete content-addressed semantic snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Literal, cast

from plantain.activities import snapshot_persistence
from plantain.activities.snapshot_bundle import (
    DEFAULT_MAX_CAPTURE_BYTES,
    StagedChunk,
    StagedSnapshot,
)
from plantain.activities.ui_errors import SnapshotError
from plantain.errors import AtomicPersistenceError
from plantain.persistence import (
    ensure_private_directory,
    ensure_private_file,
    open_binary_read_no_follow,
    private_file_lock,
)

JsonObject = dict[str, Any]
ChunkDescriptor = tuple[str, str, int]
RegistrationStatus = Literal["reused", "created", "updated", "cross_build_updated"]
EvidenceState = Literal["verified", "diagnostic"]
DiagnosticStage = Literal["action", "verification"]
REGISTRY_SCHEMA_VERSION = "4.0"
_MANIFEST_SCHEMA_VERSION = "3.0"
_VERIFY_BLOCK_BYTES = 1_048_576
MAX_VERIFIED_SNAPSHOT_EXCERPT_BYTES = 262_144
MAX_EVIDENCE_ELEMENT_TYPES = 32
MAX_EVIDENCE_KEY_IDS = 256
_CHUNK_FILE = re.compile(r"^chunks/([0-9a-f]{2})/([0-9a-f]{64})\.(json|txt)$")


@dataclass(frozen=True, slots=True)
class PendingSnapshot:
    """One complete capture staged for frozen-baseline comparison."""

    staged: StagedSnapshot
    activity: str
    evidence_state: EvidenceState = "verified"
    failure_stage: DiagnosticStage | None = None

    def __post_init__(self) -> None:
        is_diagnostic = self.evidence_state == "diagnostic"
        if is_diagnostic != (self.failure_stage is not None):
            raise ValueError("Diagnostic snapshot state requires exactly one failure stage")


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    canonical_file: str
    status: RegistrationStatus
    activity: str
    evidence_state: EvidenceState = "verified"
    failure_stage: DiagnosticStage | None = None


@dataclass(frozen=True, slots=True)
class VerifiedSnapshotSummary:
    """Immutable backend-only metadata for one verified canonical snapshot."""

    canonical_file: str
    url: str = field(repr=False)
    page_title: str = field(repr=False)
    activities: tuple[str, ...]
    normalized_key: str
    structural_digest: str
    element_counts: tuple[tuple[str, int], ...]
    key_ids: tuple[str, ...] = field(repr=False)
    last_updated: str
    summary_limited: bool = False


@dataclass(frozen=True, slots=True)
class VerifiedSnapshotExcerpt:
    """One integrity-verified bounded DOM excerpt for agent evidence."""

    summary: VerifiedSnapshotSummary
    content: str = field(repr=False)
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshotExcerpt:
    """One exact integrity-verified diagnostic retained for a bounded repair."""

    canonical_file: str
    activity: str
    failure_stage: DiagnosticStage
    url: str = field(repr=False)
    page_title: str = field(repr=False)
    content: str = field(repr=False)
    truncated: bool = False


class SnapshotRegistrationCancellationError(SnapshotError):
    """Raised when cancellation wins before the canonical registry commit."""


@dataclass(slots=True)
class SnapshotRegistrationCancellation:
    """Thread-safe cooperative cancellation for one registration transaction."""

    _event: Event = field(default_factory=Event)

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise SnapshotRegistrationCancellationError(
                "Snapshot registration was cancelled before commit"
            )


@dataclass(slots=True)
class _MutationJournal:
    originals: dict[Path, bytes | None] = field(default_factory=dict)

    def remember(self, path: Path) -> None:
        if path in self.originals:
            return
        try:
            if path.exists():
                ensure_private_file(path)
                self.originals[path] = _read_private_bytes(
                    path,
                    label="Snapshot transaction source",
                )
            else:
                self.originals[path] = None
        except (OSError, AtomicPersistenceError) as exc:
            raise SnapshotError("Unable to journal a canonical snapshot mutation") from exc

    def rollback(self) -> None:
        failures: list[Exception] = []
        for path, original in reversed(tuple(self.originals.items())):
            try:
                if original is None:
                    snapshot_persistence.unlink_durable(path)
                else:
                    snapshot_persistence.write_bytes_atomic(path, original)
            except Exception as exc:  # noqa: BLE001 - every mutation must be restored.
                failures.append(exc)
        if failures:
            raise SnapshotError("Unable to roll back canonical snapshot mutations") from (
                ExceptionGroup("snapshot rollback failures", failures)
            )


class CompleteSnapshotRegistry:
    """Canonicalize complete bundles against one immutable pre-batch baseline."""

    def __init__(self, snapshots_dir: Path) -> None:
        self._root = snapshots_dir.absolute()
        self._registry_path = self._root / "registry.json"
        self._history_dir = self._root / "history"
        self._lock_path = self._root / ".registry.lock"

    def register_batch(
        self,
        pending: Iterable[PendingSnapshot],
        *,
        cancellation: SnapshotRegistrationCancellation | None = None,
    ) -> list[RegistrationResult]:
        items = list(pending)
        if not items:
            return []
        cancellation = cancellation or SnapshotRegistrationCancellation()
        results: list[RegistrationResult] | None = None
        operation_error: Exception | None = None
        try:
            cancellation.raise_if_cancelled()
            ensure_private_directory(self._root)
            ensure_private_directory(self._history_dir)
            with private_file_lock(self._lock_path, timeout=30):
                cancellation.raise_if_cancelled()
                results = self._register_locked(items, cancellation)
        except Exception as exc:  # noqa: BLE001 - cleanup must run after every failure.
            operation_error = exc

        cleanup_errors: list[Exception] = []
        for item in items:
            try:
                item.staged.cleanup()
            except Exception as exc:  # noqa: BLE001 - every private stage must be attempted.
                cleanup_errors.append(exc)
        if operation_error is not None and not cleanup_errors:
            raise operation_error
        if operation_error is not None or cleanup_errors:
            failures = ([operation_error] if operation_error is not None else []) + cleanup_errors
            raise SnapshotError("Snapshot registration or staging cleanup failed") from (
                ExceptionGroup("snapshot registration failures", failures)
            )
        if results is None:
            raise SnapshotError("Snapshot registration produced no result")
        return results

    def resolve_activity(self, activity: str) -> Path:
        with private_file_lock(self._lock_path, timeout=30):
            entries = self._read_registry()["entries"]
            matches = [entry for entry in entries if activity in entry.get("activities", [])]
            if not matches:
                raise SnapshotError(
                    f"No snapshot found for activity '{activity}'. "
                    "Run the discovery workflow first."
                )
            if len(matches) > 1:
                files = sorted(str(entry.get("canonicalFile")) for entry in matches)
                raise SnapshotError(
                    f"Activity '{activity}' resolves to multiple DOM states: {', '.join(files)}"
                )
            path, _ = self._validated_entry(matches[0], expected_state="verified")
            return path

    def resolve_filename(self, filename: str) -> Path:
        with private_file_lock(self._lock_path, timeout=30):
            entries = self._read_registry()["entries"]
            for entry in entries:
                deprecated = entry.get("deprecated")
                if not isinstance(deprecated, list) or not all(
                    isinstance(alias, str) for alias in deprecated
                ):
                    raise SnapshotError("Snapshot registry entry has invalid deprecated aliases")
                if filename == entry.get("canonicalFile") or filename in deprecated:
                    path, _ = self._validated_entry(entry)
                    return path
        raise SnapshotError(f"Snapshot '{filename}' is not registered")

    def verified_summaries(self) -> tuple[VerifiedSnapshotSummary, ...]:
        """Return bounded metadata for every verified registry entry."""

        with private_file_lock(self._lock_path, timeout=30):
            entries = self._read_registry()["entries"]
            return tuple(
                self._verified_summary(entry)
                for entry in entries
                if entry.get("evidenceState") == "verified"
            )

    def read_verified_excerpt(
        self,
        canonical_file: str,
        *,
        max_bytes: int,
    ) -> VerifiedSnapshotExcerpt:
        """Verify one complete bundle while retaining bounded DOM evidence."""

        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 0 < max_bytes <= MAX_VERIFIED_SNAPSHOT_EXCERPT_BYTES
        ):
            raise ValueError("max_bytes is outside the verified snapshot excerpt bounds")
        with private_file_lock(self._lock_path, timeout=30):
            entries = self._read_registry()["entries"]
            matches = [entry for entry in entries if entry.get("canonicalFile") == canonical_file]
            if len(matches) != 1:
                raise SnapshotError("Verified snapshot selection is no longer available")
            summary = self._verified_summary(matches[0])
            _path, manifest, descriptors = self._validated_manifest(
                matches[0],
                expected_state="verified",
            )
            content, truncated = self._verified_dom_excerpt(
                manifest,
                descriptors,
                max_bytes,
            )
        return VerifiedSnapshotExcerpt(
            summary=summary,
            content=_decode_verified_excerpt(content, truncated=truncated),
            truncated=truncated,
        )

    def read_diagnostic_excerpt(
        self,
        canonical_file: str,
        activity: str,
        failure_stage: DiagnosticStage,
        *,
        max_bytes: int,
    ) -> DiagnosticSnapshotExcerpt:
        """Verify and bound the exact diagnostic named by a current failure."""

        _validate_excerpt_byte_limit(max_bytes)
        with private_file_lock(self._lock_path, timeout=30):
            entry = _diagnostic_read_entry(
                self._read_registry(),
                canonical_file,
                activity,
                failure_stage,
            )
            _path, manifest, descriptors = self._validated_manifest(
                entry,
                expected_state="diagnostic",
            )
            if manifest.get("failureStage") != failure_stage:
                raise SnapshotError("Diagnostic snapshot failure stage does not match")
            content, truncated = self._verified_dom_excerpt(
                manifest,
                descriptors,
                max_bytes,
            )
        return DiagnosticSnapshotExcerpt(
            canonical_file=canonical_file,
            activity=activity,
            failure_stage=failure_stage,
            url=_required_string(entry, "url"),
            page_title=_required_string(entry, "pageTitle"),
            content=_decode_verified_excerpt(content, truncated=truncated),
            truncated=truncated,
        )

    def promote_diagnostic(
        self,
        filename: str,
        activity: str,
        *,
        cancellation: SnapshotRegistrationCancellation | None = None,
    ) -> RegistrationResult:
        """Promote verification evidence only after every assertion passes."""

        cancellation = cancellation or SnapshotRegistrationCancellation()
        ensure_private_directory(self._root)
        with private_file_lock(self._lock_path, timeout=30):
            cancellation.raise_if_cancelled()
            document = deepcopy(self._read_registry())
            manifest_path, manifest = self._prepare_diagnostic_promotion(
                document,
                filename,
                activity,
            )
            self._commit_promotion(document, manifest_path, manifest, cancellation)
        return RegistrationResult(filename, "created", activity)

    def _register_locked(
        self,
        items: list[PendingSnapshot],
        cancellation: SnapshotRegistrationCancellation,
    ) -> list[RegistrationResult]:
        cancellation.raise_if_cancelled()
        baseline_document = self._read_registry()
        baseline = deepcopy(baseline_document["entries"])
        working = deepcopy(baseline_document)
        journal = _MutationJournal()
        deletions: list[Path] = []
        try:
            results = []
            for item in items:
                cancellation.raise_if_cancelled()
                results.append(
                    self._stage_one(item, baseline, working["entries"], journal, deletions)
                )
                cancellation.raise_if_cancelled()
            for obsolete in dict.fromkeys(deletions):
                cancellation.raise_if_cancelled()
                journal.remember(obsolete)
                snapshot_persistence.unlink_durable(obsolete, missing_ok=False)
            working["lastUpdated"] = _now()
            journal.remember(self._registry_path)
            cancellation.raise_if_cancelled()
            self._write_registry(working)
            cancellation.raise_if_cancelled()
        except Exception as exc:
            try:
                journal.rollback()
            except SnapshotError as rollback_exc:
                raise SnapshotError(
                    "Snapshot registration failed and rollback could not restore state"
                ) from ExceptionGroup(
                    "snapshot transaction and rollback failures",
                    [exc, rollback_exc],
                )
            raise
        return results

    def _prepare_diagnostic_promotion(
        self,
        document: JsonObject,
        filename: str,
        activity: str,
    ) -> tuple[Path, JsonObject]:
        entry = _promotion_entry(document, filename, activity)
        manifest_path, value = self._validated_entry(entry)
        if value.get("evidenceState") != "diagnostic" or value.get("failureStage") != (
            "verification"
        ):
            raise SnapshotError("Snapshot manifest is not verification diagnostic evidence")
        _mark_verified(entry, value, activity, _now())
        document["lastUpdated"] = entry["lastUpdated"]
        return manifest_path, value

    def _validated_entry(
        self,
        entry: Mapping[str, Any],
        *,
        expected_state: EvidenceState | None = None,
    ) -> tuple[Path, JsonObject]:
        path, value, descriptors = self._validated_manifest(
            entry,
            expected_state=expected_state,
        )
        for descriptor in descriptors:
            _verify_file(
                self._resolve_relative(descriptor[0]),
                descriptor,
                label="Canonical snapshot chunk",
            )
        return path, value

    def _validated_manifest(
        self,
        entry: Mapping[str, Any],
        *,
        expected_state: EvidenceState | None,
    ) -> tuple[Path, JsonObject, list[ChunkDescriptor]]:
        canonical = entry.get("canonicalFile")
        evidence_state = entry.get("evidenceState")
        if not isinstance(canonical, str) or evidence_state not in ("verified", "diagnostic"):
            raise SnapshotError("Snapshot registry entry is invalid")
        if expected_state is not None and evidence_state != expected_state:
            raise SnapshotError("Snapshot registry entry has an invalid evidence state")
        path = self._manifest_path(canonical)
        value = _read_private_json(path, label="Snapshot manifest")
        if not isinstance(value, dict):
            raise SnapshotError("Snapshot manifest must be a mapping")
        descriptors = _validate_manifest(value)
        if value.get("evidenceState") != evidence_state or any(
            value.get(key) != entry.get(key) for key in ("url", "pageTitle", "structuralSummary")
        ):
            raise SnapshotError("Snapshot registry and manifest metadata do not match")
        return path, value, descriptors

    def _verified_summary(
        self,
        entry: Mapping[str, Any],
    ) -> VerifiedSnapshotSummary:
        canonical = _required_string(entry, "canonicalFile")
        self._manifest_path(canonical)
        structure = _required_mapping(entry, "structuralSummary")
        counts, counts_limited = _summary_counts(structure)
        key_ids, keys_limited = _summary_key_ids(structure)
        encoded = json.dumps(structure, sort_keys=True, separators=(",", ":")).encode()
        return VerifiedSnapshotSummary(
            canonical_file=canonical,
            url=_required_string(entry, "url"),
            page_title=_required_string(entry, "pageTitle"),
            activities=_verified_activities(entry),
            normalized_key=_required_string(structure, "normalizedKey"),
            structural_digest=hashlib.sha256(encoded).hexdigest(),
            element_counts=counts,
            key_ids=key_ids,
            last_updated=_required_string(entry, "lastUpdated"),
            summary_limited=counts_limited or keys_limited,
        )

    def _verified_dom_excerpt(
        self,
        manifest: Mapping[str, Any],
        descriptors: list[ChunkDescriptor],
        max_bytes: int,
    ) -> tuple[bytes, bool]:
        content = _required_mapping(manifest, "content")
        dom_descriptors = _chunk_descriptors(content, "dom")
        dom_files = {descriptor[0] for descriptor in dom_descriptors}
        retained = bytearray()
        for descriptor in descriptors:
            remaining = max_bytes - len(retained)
            keep = min(remaining, descriptor[2]) if descriptor[0] in dom_files else 0
            retained.extend(
                _verify_file(
                    self._resolve_relative(descriptor[0]),
                    descriptor,
                    label="Canonical snapshot chunk",
                    retain_bytes=keep,
                )
            )
        total_dom_bytes = sum(descriptor[2] for descriptor in dom_descriptors)
        return bytes(retained), total_dom_bytes > len(retained)

    def _commit_promotion(
        self,
        document: JsonObject,
        manifest_path: Path,
        manifest: JsonObject,
        cancellation: SnapshotRegistrationCancellation,
    ) -> None:
        journal = _MutationJournal()
        try:
            journal.remember(manifest_path)
            snapshot_persistence.write_json_atomic(manifest_path, manifest)
            cancellation.raise_if_cancelled()
            journal.remember(self._registry_path)
            self._write_registry(document)
            cancellation.raise_if_cancelled()
        except Exception as exc:
            try:
                journal.rollback()
            except SnapshotError as rollback_exc:
                raise SnapshotError(
                    "Snapshot promotion failed and rollback could not restore state"
                ) from ExceptionGroup("snapshot promotion failures", [exc, rollback_exc])
            raise

    def _stage_one(
        self,
        pending: PendingSnapshot,
        baseline: list[JsonObject],
        working: list[JsonObject],
        journal: _MutationJournal,
        deletions: list[Path],
    ) -> RegistrationResult:
        self._validate_staged(pending.staged)
        manifest = pending.staged.manifest
        url = _required_string(manifest, "url")
        title = _required_string(manifest, "pageTitle")
        summary = _required_mapping(manifest, "structuralSummary")
        manifest["evidenceState"] = pending.evidence_state
        if pending.failure_stage is None:
            manifest.pop("failureStage", None)
        else:
            manifest["failureStage"] = pending.failure_stage
        if pending.evidence_state == "diagnostic":
            return self._stage_diagnostic(
                pending,
                working,
                journal,
                title=title,
                summary=summary,
            )
        exact = [
            entry
            for entry in baseline
            if entry.get("url") == url and entry.get("pageTitle") == title
        ]
        duplicate = next((entry for entry in exact if _same_structure(summary, entry)), None)
        if duplicate is not None:
            target = _working_entry(working, str(duplicate["canonicalFile"]))
            _add_activity(target, pending.activity)
            return RegistrationResult(str(target["canonicalFile"]), "reused", pending.activity)

        normalized_key = str(summary.get("normalizedKey") or "")
        cross_build = next(
            (
                entry
                for entry in baseline
                if normalized_key
                and entry.get("url") != url
                and _entry_summary(entry).get("normalizedKey") == normalized_key
                and _same_structure(summary, entry)
            ),
            None,
        )
        if cross_build is not None:
            return self._replace_cross_build(
                pending,
                cross_build,
                working,
                journal,
                deletions,
                title=title,
                url=url,
                summary=summary,
            )

        activity_entries = [
            entry for entry in baseline if pending.activity in entry.get("activities", [])
        ]
        if activity_entries:
            eligible = [
                entry
                for entry in activity_entries
                if entry.get("url") == url and entry.get("pageTitle") == title
            ]
            candidates = eligible or activity_entries
            if len(candidates) != 1:
                raise SnapshotError(
                    f"Activity '{pending.activity}' has multiple canonical states; "
                    "use a distinct activity name for this page state"
                )
            target = _working_entry(working, str(candidates[0]["canonicalFile"]))
            canonical = str(target["canonicalFile"])
            self._archive(target, journal)
            self._persist_bundle(pending.staged, canonical, journal)
            target.update(
                {
                    "url": url,
                    "pageTitle": title,
                    "structuralSummary": dict(summary),
                    "lastUpdated": _now(),
                    "changeNote": _describe_change(_entry_summary(candidates[0]), summary),
                }
            )
            return RegistrationResult(canonical, "updated", pending.activity)

        canonical = self._new_filename(
            title,
            summary=summary,
            reserved={str(entry["canonicalFile"]) for entry in working},
        )
        self._persist_bundle(pending.staged, canonical, journal)
        working.append(
            {
                "canonicalFile": canonical,
                "url": url,
                "pageTitle": title,
                "activities": [pending.activity],
                "diagnostics": [],
                "evidenceState": "verified",
                "structuralSummary": dict(summary),
                "lastUpdated": _now(),
                "deprecated": [],
            }
        )
        return RegistrationResult(canonical, "created", pending.activity)

    def _stage_diagnostic(
        self,
        pending: PendingSnapshot,
        working: list[JsonObject],
        journal: _MutationJournal,
        *,
        title: str,
        summary: Mapping[str, Any],
    ) -> RegistrationResult:
        failure_stage = pending.failure_stage
        if failure_stage is None:
            raise SnapshotError("Diagnostic snapshot requires a failure stage")
        canonical = self._new_filename(
            f"{title} Diagnostic",
            summary=summary,
            reserved={str(entry["canonicalFile"]) for entry in working},
        )
        self._persist_bundle(pending.staged, canonical, journal)
        working.append(_diagnostic_entry(pending, canonical, _now()))
        return RegistrationResult(
            canonical,
            "created",
            pending.activity,
            evidence_state="diagnostic",
            failure_stage=failure_stage,
        )

    def _replace_cross_build(
        self,
        pending: PendingSnapshot,
        baseline_entry: JsonObject,
        working: list[JsonObject],
        journal: _MutationJournal,
        deletions: list[Path],
        *,
        title: str,
        url: str,
        summary: Mapping[str, Any],
    ) -> RegistrationResult:
        old_file = str(baseline_entry["canonicalFile"])
        target = _working_entry(working, old_file)
        new_file = self._new_filename(
            title,
            summary=summary,
            reserved={str(entry["canonicalFile"]) for entry in working},
        )
        self._archive(target, journal)
        self._persist_bundle(pending.staged, new_file, journal)
        deletions.append(self._manifest_path(old_file))
        target.update(
            {
                "canonicalFile": new_file,
                "url": url,
                "pageTitle": title,
                "activities": sorted(
                    {str(value) for value in target.get("activities", [])} | {pending.activity}
                ),
                "structuralSummary": dict(summary),
                "lastUpdated": _now(),
                "deprecated": list(dict.fromkeys([*target.get("deprecated", []), old_file])),
                "changeNote": "Replaced equivalent snapshot from an older URL build",
            }
        )
        return RegistrationResult(new_file, "cross_build_updated", pending.activity)

    def _persist_bundle(
        self,
        staged: StagedSnapshot,
        canonical: str,
        journal: _MutationJournal,
    ) -> None:
        for chunk in staged.chunks:
            self._materialize_chunk(chunk, journal)
        destination = self._manifest_path(canonical)
        journal.remember(destination)
        snapshot_persistence.write_json_atomic(destination, staged.manifest)

    def _materialize_chunk(
        self,
        chunk: StagedChunk,
        journal: _MutationJournal,
    ) -> None:
        match = _CHUNK_FILE.fullmatch(chunk.relative_file)
        if match is None or match.group(1) != chunk.sha256[:2] or match.group(2) != chunk.sha256:
            raise SnapshotError("Snapshot chunk has an invalid content-addressed path")
        destination = self._resolve_relative(chunk.relative_file)
        ensure_private_directory(destination.parent)
        if destination.exists():
            try:
                ensure_private_file(destination)
            except AtomicPersistenceError as exc:
                raise SnapshotError("Existing snapshot chunk is unsafe") from exc
            if (
                destination.stat().st_size != chunk.size_bytes
                or snapshot_persistence.digest_file(destination) != chunk.sha256
            ):
                raise SnapshotError("Existing content-addressed snapshot chunk is corrupt")
            return
        journal.remember(destination)
        snapshot_persistence.copy_atomic(chunk.path, destination)
        if (
            destination.stat().st_size != chunk.size_bytes
            or snapshot_persistence.digest_file(destination) != chunk.sha256
        ):
            raise SnapshotError("Persisted snapshot chunk failed integrity verification")

    def _validate_staged(self, staged: StagedSnapshot) -> None:
        descriptors = _validate_manifest(staged.manifest)
        referenced = {descriptor[0] for descriptor in descriptors}
        available = {chunk.relative_file: chunk for chunk in staged.chunks}
        if referenced != set(available) or len(available) != len(staged.chunks):
            raise SnapshotError("Snapshot manifest and staged chunks do not match")
        for filename, digest, byte_count in descriptors:
            chunk = available[filename]
            if chunk.sha256 != digest or chunk.size_bytes != byte_count:
                raise SnapshotError("Snapshot manifest and staged chunks do not match")
        for filename, chunk in available.items():
            if chunk.path.is_symlink():
                raise SnapshotError("Snapshot staging cannot contain symbolic links")
            if chunk.path.absolute().parent != staged.staging_dir.absolute():
                raise SnapshotError("Snapshot chunk escapes its private staging directory")
            _verify_file(
                chunk.path,
                (filename, chunk.sha256, chunk.size_bytes),
                label="Staged snapshot chunk",
            )

    def _archive(
        self,
        entry: Mapping[str, Any],
        journal: _MutationJournal,
    ) -> None:
        canonical = str(entry["canonicalFile"])
        source = self._manifest_path(canonical)
        if not source.exists():
            raise SnapshotError(f"Canonical snapshot '{canonical}' is missing from disk")
        source, _ = self._validated_entry(entry)
        timestamp = re.sub(r"[^0-9]", "", str(entry.get("lastUpdated") or _now()))[:20]
        archive_base = f"{Path(canonical).stem}_{timestamp}"
        archive = self._history_dir / f"{archive_base}.semantic.json"
        suffix = 2
        while archive.exists():
            archive = self._history_dir / f"{archive_base}_{suffix}.semantic.json"
            suffix += 1
        journal.remember(archive)
        snapshot_persistence.copy_atomic(source, archive)

    def _new_filename(
        self,
        title: str,
        *,
        summary: Mapping[str, Any],
        reserved: set[str],
    ) -> str:
        base = _slug(title) or "untitled_page"
        candidate = f"{base}.semantic.json"
        if candidate not in reserved and not self._manifest_path(candidate).exists():
            return candidate
        digest = hashlib.sha256(
            json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:10]
        state_base = f"{base}_state_{digest}"
        candidate = f"{state_base}.semantic.json"
        suffix = 2
        while candidate in reserved or self._manifest_path(candidate).exists():
            candidate = f"{state_base}_{suffix}.semantic.json"
            suffix += 1
        return candidate

    def _read_registry(self) -> JsonObject:
        try:
            ensure_private_file(self._registry_path, missing_ok=True)
        except (OSError, AtomicPersistenceError) as exc:
            raise SnapshotError("Snapshot registry is unreadable or invalid JSON") from exc
        if not self._registry_path.exists():
            return {
                "schemaVersion": REGISTRY_SCHEMA_VERSION,
                "entries": [],
                "lastUpdated": None,
            }
        try:
            raw = _read_private_bytes(self._registry_path, label="Snapshot registry")
        except SnapshotError as exc:
            raise SnapshotError("Snapshot registry is unreadable or invalid JSON") from exc
        if not raw:
            return {
                "schemaVersion": REGISTRY_SCHEMA_VERSION,
                "entries": [],
                "lastUpdated": None,
            }
        try:
            data: object = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise SnapshotError("Snapshot registry is unreadable or invalid JSON") from exc
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("entries"), list)
            or not all(isinstance(entry, dict) for entry in data["entries"])
        ):
            raise SnapshotError("Snapshot registry must contain an entries array")
        if data.get("schemaVersion") != REGISTRY_SCHEMA_VERSION:
            raise SnapshotError("Snapshot registry uses an unsupported schema version")
        return cast("JsonObject", data)

    def _write_registry(self, value: Mapping[str, Any]) -> None:
        snapshot_persistence.write_json_atomic(self._registry_path, value)

    def _manifest_path(self, filename: str) -> Path:
        if (
            Path(filename).name != filename
            or "\\" in filename
            or not filename.endswith(".semantic.json")
        ):
            raise SnapshotError("Snapshot filename is unsafe or has an invalid extension")
        return self._resolve_relative(filename)

    def _resolve_relative(self, relative: str) -> Path:
        parts = relative.split("/")
        if not relative or any(part in ("", ".", "..") for part in parts):
            raise SnapshotError("Snapshot path escapes its configured directory")
        return self._root.joinpath(*parts)


def _same_structure(summary: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    existing = _entry_summary(entry)
    return (
        summary.get("normalizedFrameUrl") == existing.get("normalizedFrameUrl")
        and summary.get("elementCounts") == existing.get("elementCounts")
        and summary.get("keyIds") == existing.get("keyIds")
        and _normalized_nested(summary.get("nestedElementCounts"))
        == _normalized_nested(existing.get("nestedElementCounts"))
    )


def _normalized_nested(value: object) -> list[tuple[str, str, str, str, str]]:
    if not isinstance(value, list):
        return []
    return [
        (
            str(item.get("frameId") or ""),
            str(item.get("parentFrameId") or ""),
            str(item.get("normalizedFrameUrl") or ""),
            json.dumps(item.get("elementCounts") or {}, sort_keys=True),
            json.dumps(item.get("nestedKeyIds") or [], sort_keys=True),
        )
        for item in value
        if isinstance(item, Mapping)
    ]


def _describe_change(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    fields = ("elementCounts", "keyIds", "nestedElementCounts", "normalizedFrameUrl")
    changes = [field for field in fields if before.get(field) != after.get(field)]
    return "Structural fields changed: " + ", ".join(changes or ["unknown"])


def _entry_summary(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    value = entry.get("structuralSummary")
    return value if isinstance(value, Mapping) else {}


def _working_entry(entries: list[JsonObject], canonical: str) -> JsonObject:
    for entry in entries:
        if entry.get("canonicalFile") == canonical:
            return entry
    raise SnapshotError(f"Registry entry '{canonical}' disappeared during update")


def _add_activity(entry: JsonObject, activity: str) -> None:
    entry["activities"] = sorted({str(value) for value in entry.get("activities", [])} | {activity})
    entry["lastUpdated"] = _now()


def _diagnostic_entry(
    pending: PendingSnapshot,
    canonical: str,
    observed_at: str,
) -> JsonObject:
    manifest = pending.staged.manifest
    return {
        "canonicalFile": canonical,
        "url": _required_string(manifest, "url"),
        "pageTitle": _required_string(manifest, "pageTitle"),
        "activities": [],
        "diagnostics": [
            {
                "activity": pending.activity,
                "failureStage": pending.failure_stage,
                "observedAt": observed_at,
            }
        ],
        "evidenceState": "diagnostic",
        "structuralSummary": dict(_required_mapping(manifest, "structuralSummary")),
        "lastUpdated": observed_at,
        "deprecated": [],
    }


def _diagnostic_read_entry(
    document: JsonObject,
    filename: str,
    activity: str,
    failure_stage: DiagnosticStage,
) -> JsonObject:
    matches = [entry for entry in document["entries"] if entry.get("canonicalFile") == filename]
    if len(matches) != 1:
        raise SnapshotError("Diagnostic snapshot is not registered exactly once")
    entry = matches[0]
    diagnostics = entry.get("diagnostics")
    record = diagnostics[0] if isinstance(diagnostics, list) and len(diagnostics) == 1 else None
    if (
        entry.get("evidenceState") != "diagnostic"
        or entry.get("activities") != []
        or not isinstance(record, Mapping)
        or record.get("activity") != activity
        or record.get("failureStage") != failure_stage
    ):
        raise SnapshotError("Snapshot is not exact diagnostic evidence for this failure")
    return cast("JsonObject", entry)


def _promotion_entry(
    document: JsonObject,
    filename: str,
    activity: str,
) -> JsonObject:
    matches = [entry for entry in document["entries"] if entry.get("canonicalFile") == filename]
    if len(matches) != 1:
        raise SnapshotError("Diagnostic snapshot is not registered exactly once")
    entry = matches[0]
    diagnostics = entry.get("diagnostics")
    record = diagnostics[0] if isinstance(diagnostics, list) and len(diagnostics) == 1 else None
    if (
        entry.get("evidenceState") != "diagnostic"
        or entry.get("activities") != []
        or not isinstance(record, Mapping)
        or record.get("activity") != activity
        or record.get("failureStage") != "verification"
    ):
        raise SnapshotError("Snapshot is not verification diagnostic evidence for this activity")
    return cast("JsonObject", entry)


def _mark_verified(
    entry: JsonObject,
    manifest: JsonObject,
    activity: str,
    observed_at: str,
) -> None:
    entry.update(
        activities=[activity],
        diagnostics=[],
        evidenceState="verified",
        lastUpdated=observed_at,
    )
    manifest["evidenceState"] = "verified"
    manifest.pop("failureStage", None)


def _validate_accessibility_content(content: Mapping[str, Any]) -> None:
    aria = content.get("aria")
    dom = content.get("dom")
    if not isinstance(aria, Mapping) or not isinstance(dom, Mapping):
        raise SnapshotError("Snapshot manifest requires DOM-sourced accessibility metadata")
    contract = (aria.get("format"), aria.get("source"), aria.get("depth"))
    if "chunks" in aria or contract != (
        "semantic-accessibility-records-v1",
        "dom",
        "unlimited",
    ):
        raise SnapshotError("Snapshot accessibility metadata must use the DOM source contract")
    aria_count = aria.get("recordCount")
    dom_count = dom.get("recordCount")
    if not _is_non_negative_int(aria_count) or not _is_non_negative_int(dom_count):
        raise SnapshotError("Snapshot accessibility record counts must be non-negative integers")
    if aria_count != dom_count:
        raise SnapshotError("Snapshot accessibility and DOM record counts do not match")


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise SnapshotError(f"Snapshot requires a non-empty '{key}'")
    return item


def _validate_excerpt_byte_limit(max_bytes: int) -> None:
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 0 < max_bytes <= MAX_VERIFIED_SNAPSHOT_EXCERPT_BYTES
    ):
        raise ValueError("max_bytes is outside the verified snapshot excerpt bounds")


def _read_private_bytes(path: Path, *, label: str) -> bytes:
    try:
        with open_binary_read_no_follow(path, private=True) as handle:
            value = handle.read(DEFAULT_MAX_CAPTURE_BYTES + 1)
    except (OSError, AtomicPersistenceError) as exc:
        raise SnapshotError(f"{label} is unreadable or unsafe") from exc
    if len(value) > DEFAULT_MAX_CAPTURE_BYTES:
        raise SnapshotError(f"{label} exceeds its byte limit")
    return value


def _verified_activities(entry: Mapping[str, Any]) -> tuple[str, ...]:
    value = entry.get("activities")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise SnapshotError("Verified snapshot activities are invalid")
    return tuple(value)


def _summary_counts(
    structure: Mapping[str, Any],
) -> tuple[tuple[tuple[str, int], ...], bool]:
    raw = _required_mapping(structure, "elementCounts")
    values: list[tuple[str, int]] = []
    for name, count in raw.items():
        if not isinstance(name, str) or not _is_non_negative_int(count):
            raise SnapshotError("Snapshot structural element counts are invalid")
        values.append((name, cast("int", count)))
    values.sort()
    return tuple(values[:MAX_EVIDENCE_ELEMENT_TYPES]), len(values) > MAX_EVIDENCE_ELEMENT_TYPES


def _summary_key_ids(
    structure: Mapping[str, Any],
) -> tuple[tuple[str, ...], bool]:
    raw = structure.get("keyIds")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise SnapshotError("Snapshot structural key identifiers are invalid")
    return tuple(raw[:MAX_EVIDENCE_KEY_IDS]), len(raw) > MAX_EVIDENCE_KEY_IDS


def _read_private_json(path: Path, *, label: str) -> object:
    raw = _read_private_bytes(path, label=label)
    try:
        value: object = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SnapshotError(f"{label} is unreadable or invalid JSON") from exc
    return value


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise SnapshotError(f"Snapshot requires a '{key}' mapping")
    return item


def _validate_manifest(manifest: Mapping[str, Any]) -> list[ChunkDescriptor]:
    if manifest.get("schemaVersion") != _MANIFEST_SCHEMA_VERSION:
        raise SnapshotError("Snapshot manifest uses an unsupported schema version")
    if manifest.get("captureComplete") is not True:
        raise SnapshotError("Incomplete snapshots cannot be registered")
    _required_string(manifest, "url")
    _required_string(manifest, "pageTitle")
    _required_mapping(manifest, "structuralSummary")
    content = _required_mapping(manifest, "content")
    _validate_accessibility_content(content)
    descriptors = [
        descriptor
        for kind in ("dom", "network")
        for descriptor in _chunk_descriptors(content, kind)
    ]
    if sum(descriptor[2] for descriptor in descriptors) > DEFAULT_MAX_CAPTURE_BYTES:
        raise SnapshotError("Snapshot manifest exceeds its content byte limit")
    return descriptors


def _chunk_descriptors(
    content: Mapping[str, Any],
    kind: str,
) -> list[ChunkDescriptor]:
    group = content.get(kind)
    if not isinstance(group, Mapping) or not isinstance(group.get("chunks"), list):
        raise SnapshotError(f"Snapshot manifest requires {kind} chunks")
    chunks = group["chunks"]
    chunk_count = group.get("chunkCount")
    if not _is_non_negative_int(chunk_count) or chunk_count != len(chunks):
        raise SnapshotError(f"Snapshot manifest has an invalid {kind} chunk count")
    return [_chunk_descriptor(descriptor) for descriptor in chunks]


def _chunk_descriptor(value: object) -> ChunkDescriptor:
    if not isinstance(value, Mapping):
        raise SnapshotError("Snapshot manifest contains an invalid chunk descriptor")
    filename = value.get("file")
    digest = value.get("sha256")
    byte_count = value.get("byteCount")
    if not isinstance(filename, str):
        raise SnapshotError("Snapshot chunk descriptor requires a file")
    match = _CHUNK_FILE.fullmatch(filename)
    if (
        match is None
        or not isinstance(digest, str)
        or match.group(1) != digest[:2]
        or match.group(2) != digest
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
        or byte_count > DEFAULT_MAX_CAPTURE_BYTES
    ):
        raise SnapshotError("Snapshot manifest contains an invalid chunk descriptor")
    return filename, digest, byte_count


def _verify_file(
    path: Path,
    descriptor: ChunkDescriptor,
    *,
    label: str,
    retain_bytes: int = 0,
) -> bytes:
    _, expected_digest, expected_size = descriptor
    retained = bytearray()
    try:
        with open_binary_read_no_follow(path, private=True) as handle:
            handle.seek(0, 2)
            actual_size = handle.tell()
            handle.seek(0)
            digest = hashlib.sha256()
            if actual_size == expected_size:
                while block := handle.read(_VERIFY_BLOCK_BYTES):
                    digest.update(block)
                    if len(retained) < retain_bytes:
                        retained.extend(block[: retain_bytes - len(retained)])
    except (OSError, AtomicPersistenceError) as exc:
        raise SnapshotError(f"{label} is unreadable or unsafe") from exc
    if actual_size != expected_size or digest.hexdigest() != expected_digest:
        raise SnapshotError(f"{label} failed integrity verification")
    return bytes(retained)


def _decode_verified_excerpt(value: bytes, *, truncated: bool) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        if not truncated or exc.end != len(value):
            raise SnapshotError("Canonical snapshot DOM content is not valid UTF-8") from exc
        return value[: exc.start].decode("utf-8")


def _chunk_references(content: Mapping[str, Any], kind: str) -> set[str]:
    return {descriptor[0] for descriptor in _chunk_descriptors(content, kind)}


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")[:100]


def _now() -> str:
    return datetime.now(UTC).isoformat()


# Transaction-facing types are explicit; validation helpers remain private implementation details.
__all__ = [
    "REGISTRY_SCHEMA_VERSION",
    "CompleteSnapshotRegistry",
    "DiagnosticSnapshotExcerpt",
    "DiagnosticStage",
    "EvidenceState",
    "PendingSnapshot",
    "RegistrationResult",
    "SnapshotRegistrationCancellation",
    "SnapshotRegistrationCancellationError",
    "VerifiedSnapshotExcerpt",
    "VerifiedSnapshotSummary",
]
