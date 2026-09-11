"""Staging primitives for complete, content-addressed semantic snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from plantain.activities.ui_errors import SnapshotError
from plantain.errors import AtomicPersistenceError
from plantain.persistence import ensure_private_directory, open_private_binary_exclusive

JsonObject = dict[str, Any]
ChunkKind = Literal["aria", "dom", "network"]
DEFAULT_TEXT_CHUNK_BYTES = 262_144
DEFAULT_RECORD_CHUNK_BYTES = 1_048_576
DEFAULT_MAX_CAPTURE_BYTES = 536_870_912
MIN_TEXT_CHUNK_BYTES = 4


@dataclass(frozen=True, slots=True)
class StagedChunk:
    """One immutable payload awaiting canonical content-addressed persistence."""

    path: Path
    relative_file: str
    sha256: str
    size_bytes: int


@dataclass(slots=True)
class StagedSnapshot:
    """Complete manifest and staged payloads owned by one capture."""

    manifest: JsonObject
    chunks: tuple[StagedChunk, ...]
    staging_dir: Path
    staging_root: Path
    _cleaned: bool = field(default=False, init=False, repr=False)

    def cleanup(self) -> None:
        """Remove only this capture's validated private staging directory."""

        if self._cleaned:
            return
        resolved = self.staging_dir.resolve()
        try:
            resolved.relative_to(self.staging_root.resolve())
        except ValueError as exc:
            raise SnapshotError("Snapshot staging path escapes its configured root") from exc
        if resolved == self.staging_root.resolve():
            raise SnapshotError("Refusing to remove the snapshot staging root")
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise SnapshotError("Unable to remove private snapshot staging data") from exc
        self._cleaned = True


class SnapshotBundleBuilder:
    """Write bounded transport chunks without imposing a total capture limit."""

    def __init__(
        self,
        snapshots_dir: Path,
        *,
        max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    ) -> None:
        if max_capture_bytes < 1:
            raise ValueError("max_capture_bytes must be positive")
        self._root = snapshots_dir.absolute()
        self._staging_root = self._root / ".staging"
        try:
            self._staging_root.relative_to(self._root)
            ensure_private_directory(self._root)
            ensure_private_directory(self._staging_root)
            self._staging_dir = Path(
                tempfile.mkdtemp(prefix="capture-", dir=self._staging_root)
            ).absolute()
            ensure_private_directory(self._staging_dir)
        except (OSError, ValueError, AtomicPersistenceError) as exc:
            raise SnapshotError("Unable to create private snapshot staging storage") from exc
        self._chunks: dict[str, StagedChunk] = {}
        self._max_capture_bytes = max_capture_bytes
        self._captured_bytes = 0
        self._finished = False

    def add_text(
        self,
        *,
        kind: ChunkKind,
        text: str,
        metadata: Mapping[str, Any],
        target_bytes: int = DEFAULT_TEXT_CHUNK_BYTES,
    ) -> list[JsonObject]:
        """Persist every character within strict UTF-8 byte-bounded chunks."""

        if target_bytes < MIN_TEXT_CHUNK_BYTES:
            raise ValueError("target_bytes must accommodate one UTF-8 code point")
        descriptors: list[JsonObject] = []
        for sequence, content in enumerate(_text_chunks(text, target_bytes), start=1):
            payload = content.encode("utf-8")
            descriptor = self._add_payload(payload, suffix="txt")
            descriptors.append(
                {
                    "kind": kind,
                    "sequence": sequence,
                    "mediaType": "text/plain; charset=utf-8",
                    "characterCount": len(content),
                    **metadata,
                    **descriptor,
                }
            )
        return descriptors

    def add_records(
        self,
        *,
        kind: ChunkKind,
        sequence: int,
        records: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
        max_payload_bytes: int = DEFAULT_RECORD_CHUNK_BYTES,
    ) -> JsonObject:
        """Persist one already-bounded record transport batch."""

        if sequence < 1:
            raise ValueError("sequence must be positive")
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        document = {
            "schemaVersion": "1.0",
            "kind": kind,
            **metadata,
            "records": list(records),
        }
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > max_payload_bytes:
            raise SnapshotError("Snapshot record batch exceeds PLANTAIN_SNAPSHOT_WORKING_SET_BYTES")
        descriptor = self._add_payload(payload, suffix="json")
        return {
            "kind": kind,
            "sequence": sequence,
            "mediaType": "application/json",
            "recordCount": len(records),
            **metadata,
            **descriptor,
        }

    def finish(self, manifest: JsonObject) -> StagedSnapshot:
        if self._finished:
            raise SnapshotError("Snapshot bundle staging has already finished")
        if manifest.get("captureComplete") is not True:
            raise SnapshotError("A snapshot manifest cannot be staged as incomplete")
        self._ensure_capacity(_json_document_bytes(manifest))
        self._finished = True
        return StagedSnapshot(
            manifest=manifest,
            chunks=tuple(self._chunks.values()),
            staging_dir=self._staging_dir,
            staging_root=self._staging_root,
        )

    def abort(self) -> None:
        if self._finished:
            return
        staged = StagedSnapshot(
            manifest={},
            chunks=(),
            staging_dir=self._staging_dir,
            staging_root=self._staging_root,
        )
        staged.cleanup()
        self._finished = True

    def _add_payload(self, payload: bytes, *, suffix: str) -> JsonObject:
        digest = hashlib.sha256(payload).hexdigest()
        relative_file = f"chunks/{digest[:2]}/{digest}.{suffix}"
        self._ensure_capacity(len(payload))
        existing = self._chunks.get(relative_file)
        if existing is None:
            staged_path = self._staging_dir / f"{len(self._chunks) + 1:08d}.{suffix}"
            try:
                with open_private_binary_exclusive(staged_path) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except (OSError, AtomicPersistenceError) as exc:
                raise SnapshotError("Unable to stage complete snapshot content") from exc
            existing = StagedChunk(
                path=staged_path,
                relative_file=relative_file,
                sha256=digest,
                size_bytes=len(payload),
            )
            self._chunks[relative_file] = existing
        self._captured_bytes += len(payload)
        return {
            "file": relative_file,
            "sha256": digest,
            "byteCount": len(payload),
        }

    def _ensure_capacity(self, additional_bytes: int) -> None:
        if self._captured_bytes + additional_bytes > self._max_capture_bytes:
            raise SnapshotError("Complete snapshot exceeds PLANTAIN_SNAPSHOT_MAX_CAPTURE_BYTES")


def _text_chunks(value: str, target_bytes: int) -> Iterator[str]:
    if not value:
        yield ""
        return
    current: list[str] = []
    current_bytes = 0
    for line in value.splitlines(keepends=True):
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > target_bytes:
            if current:
                yield "".join(current)
                current = []
                current_bytes = 0
            yield from _oversized_text_chunks(line, target_bytes)
            continue
        if current and current_bytes + line_bytes > target_bytes:
            yield "".join(current)
            current = []
            current_bytes = 0
        current.append(line)
        current_bytes += line_bytes
    if current:
        yield "".join(current)


def _oversized_text_chunks(value: str, target_bytes: int) -> Iterator[str]:
    current: list[str] = []
    current_bytes = 0
    for character in value:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > target_bytes:
            yield "".join(current)
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        yield "".join(current)


def _json_document_bytes(value: Mapping[str, Any]) -> int:
    encoder = json.JSONEncoder(indent=2, ensure_ascii=False)
    return 1 + sum(len(fragment.encode("utf-8")) for fragment in encoder.iterencode(value))


__all__ = ["SnapshotBundleBuilder", "StagedChunk", "StagedSnapshot"]
# Snapshot bundle exports are intentionally explicit.
