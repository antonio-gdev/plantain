"""Snapshot-specific adapters over framework atomic persistence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from plantain import persistence
from plantain.activities.ui_errors import SnapshotError
from plantain.errors import AtomicPersistenceError


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    """Stream JSON through a durable temporary file, then atomically replace."""

    try:
        persistence.write_json_atomic(path, value)
    except AtomicPersistenceError as exc:
        raise SnapshotError("Atomic snapshot JSON persistence failed") from exc


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Durably replace a file with already-serialized bytes."""

    try:
        persistence.write_bytes_atomic(path, payload)
    except AtomicPersistenceError as exc:
        raise SnapshotError("Atomic snapshot persistence failed") from exc


def copy_atomic(source: Path, destination: Path) -> None:
    """Copy through a durable sibling temporary file, then atomically replace."""

    try:
        persistence.copy_atomic(source, destination)
    except AtomicPersistenceError as exc:
        raise SnapshotError("Atomic snapshot copy failed") from exc


def unlink_durable(path: Path, *, missing_ok: bool = True) -> None:
    """Durably remove a snapshot artifact when present."""

    try:
        persistence.unlink_durable(path, missing_ok=missing_ok)
    except AtomicPersistenceError as exc:
        raise SnapshotError("Durable snapshot deletion failed") from exc


def digest_file(path: Path) -> str:
    """Hash a file incrementally without loading it into memory."""

    try:
        return persistence.digest_file(path)
    except AtomicPersistenceError as exc:
        raise SnapshotError("Unable to verify snapshot chunk integrity") from exc


__all__ = [
    "copy_atomic",
    "digest_file",
    "unlink_durable",
    "write_bytes_atomic",
    "write_json_atomic",
]
