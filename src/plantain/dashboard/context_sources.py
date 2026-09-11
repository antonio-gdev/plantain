"""Private, bounded local context sources for dashboard agent workflows."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from filelock import Timeout as FileLockTimeout
from pydantic import Field, ValidationError, model_validator

from plantain.errors import AtomicPersistenceError, PlantainError
from plantain.models.common import StrictModel
from plantain.persistence import (
    open_binary_read_no_follow,
    private_file_lock,
    write_json_atomic,
)

CONTEXT_SCHEMA_VERSION = "1.0"
CONTEXT_MANIFEST_MAX_BYTES = 2_097_152
CONTEXT_LOCK_TIMEOUT_SECONDS = 10.0
MAX_STORED_CONTEXT_SOURCES = 256
MAX_DISCOVERED_ENTRIES = 20_000
MAX_INDEXED_FILES_PER_SOURCE = 4_000
MAX_SOURCE_PATH_LENGTH = 4_096
MAX_RELATIVE_PATH_LENGTH = 512
MAX_SOURCE_LABEL_LENGTH = 120
_SOURCE_ID_LENGTH = 64
_SOURCE_ID_PATTERN = rf"^[0-9a-f]{{{_SOURCE_ID_LENGTH}}}$"
_MANIFEST_DIRECTORY = Path(".plantain/dashboard")
_MANIFEST_NAME = "context-sources.json"
_LOCK_NAME = "context-sources.lock"

_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".next",
        ".nox",
        ".nuxt",
        ".plantain",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "generated",
        "htmlcov",
        "node_modules",
        "output",
        "snapshots",
        "target",
        "vendor",
        "venv",
    }
)
_EXCLUDED_FILES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "package-lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "service-account.json",
        "uv.lock",
        "yarn.lock",
    }
)
_EXCLUDED_SUFFIXES = frozenset(
    {
        ".crt",
        ".der",
        ".jks",
        ".key",
        ".p12",
        ".pem",
        ".pfx",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cfg",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".feature",
        ".fs",
        ".fsx",
        ".go",
        ".gql",
        ".graphql",
        ".h",
        ".hpp",
        ".htm",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".json5",
        ".jsx",
        ".kt",
        ".kts",
        ".less",
        ".md",
        ".mdx",
        ".php",
        ".properties",
        ".proto",
        ".ps1",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".rst",
        ".sass",
        ".scala",
        ".scss",
        ".sh",
        ".sql",
        ".svelte",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_FILENAMES = frozenset(
    {
        "dockerfile",
        "gemfile",
        "makefile",
        "procfile",
        "readme",
    }
)
BoundedRelativePath = Annotated[
    str,
    Field(min_length=1, max_length=MAX_RELATIVE_PATH_LENGTH),
]


class ContextSourceError(PlantainError):
    """Raised when local context cannot be indexed or persisted safely."""


class ContextSourceKind(StrEnum):
    """Supported explicit local evidence categories."""

    APPLICATION = "application"
    REQUIREMENTS = "requirements"
    API_CONTRACT = "api_contract"


class _ContextSourceRecord(StrictModel):
    source_id: str = Field(alias="sourceId", pattern=_SOURCE_ID_PATTERN)
    kind: ContextSourceKind
    path: str = Field(min_length=1, max_length=MAX_SOURCE_PATH_LENGTH)
    label: str = Field(min_length=1, max_length=MAX_SOURCE_LABEL_LENGTH)
    is_directory: bool = Field(alias="isDirectory")
    files: list[BoundedRelativePath] = Field(
        min_length=1,
        max_length=MAX_INDEXED_FILES_PER_SOURCE,
    )
    partial: bool = False

    @model_validator(mode="after")
    def valid_paths(self) -> Self:
        """Keep private manifest paths absolute, unique, and traversal-free."""

        if not Path(self.path).is_absolute():
            raise ValueError("Context source path must be absolute")
        if len(self.files) != len(set(self.files)):
            raise ValueError("Context source files must be unique")
        for value in self.files:
            relative = PurePosixPath(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Context source file path is invalid")
            if not _supported_file(relative.name):
                raise ValueError("Context source file type is unsupported")
        return self


class _ContextSourceManifest(StrictModel):
    schema_version: Literal["1.0"] = Field(
        default="1.0",
        alias="schemaVersion",
    )
    sources: list[_ContextSourceRecord] = Field(
        default_factory=list,
        max_length=MAX_STORED_CONTEXT_SOURCES,
    )

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        """Reject ambiguous identifiers or duplicate source identities."""

        identifiers = [source.source_id for source in self.sources]
        identities = [(source.kind, source.path) for source in self.sources]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Context source identifiers must be unique")
        if len(identities) != len(set(identities)):
            raise ValueError("Context source identities must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ContextSourceSummary:
    """Browser-safe metadata for one explicitly attached local source."""

    source_id: str
    kind: ContextSourceKind
    label: str
    indexed_file_count: int
    partial: bool
    available: bool


@dataclass(frozen=True, slots=True)
class ContextSourceDescriptor:
    """Backend-only source location used by the bounded context selector."""

    source_id: str
    kind: ContextSourceKind
    path: Path
    label: str
    is_directory: bool
    files: tuple[str, ...]
    partial: bool


@dataclass(frozen=True, slots=True)
class ContextCatalog:
    """Browser-safe source catalog and agent-readiness projection."""

    sources: tuple[ContextSourceSummary, ...]
    notice: str
    focus_source_id: str | None = None

    @property
    def application_source_available(self) -> bool:
        return self._has_available(ContextSourceKind.APPLICATION)

    @property
    def requirements_available(self) -> bool:
        return self._has_available(ContextSourceKind.REQUIREMENTS)

    @property
    def api_schema_available(self) -> bool:
        return self._has_available(ContextSourceKind.API_CONTRACT)

    def _has_available(self, kind: ContextSourceKind) -> bool:
        return any(source.kind is kind and source.available for source in self.sources)


@dataclass(frozen=True, slots=True)
class _SourceInventory:
    is_directory: bool
    files: tuple[str, ...]
    partial: bool


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    name: str
    is_directory: bool
    is_file: bool
    is_symlink: bool


def load_context_catalog(
    project_root: Path,
    *,
    focus_source_id: str | None = None,
) -> ContextCatalog:
    """Load a bounded browser-safe projection without exposing source paths."""

    manifest_path, _ = _storage_paths(project_root)
    return _catalog(
        _read_manifest(manifest_path),
        focus_source_id=focus_source_id,
    )


def load_context_descriptors(
    project_root: Path,
) -> tuple[ContextSourceDescriptor, ...]:
    """Load private source locations for backend-only context selection."""

    manifest_path, _ = _storage_paths(project_root)
    return tuple(
        ContextSourceDescriptor(
            source_id=source.source_id,
            kind=source.kind,
            path=Path(source.path),
            label=source.label,
            is_directory=source.is_directory,
            files=tuple(source.files),
            partial=source.partial,
        )
        for source in _read_manifest(manifest_path).sources
    )


def add_context_source(
    project_root: Path,
    kind: ContextSourceKind,
    raw_path: str,
) -> ContextCatalog:
    """Index and persist one explicitly selected local source."""

    selected = _selected_path(project_root, raw_path)
    inventory = _inventory(selected)
    record = _ContextSourceRecord(
        source_id=_source_id(kind, selected),
        kind=kind,
        path=str(selected),
        label=_source_label(selected),
        is_directory=inventory.is_directory,
        files=list(inventory.files),
        partial=inventory.partial,
    )
    manifest_path, lock_path = _storage_paths(project_root)
    try:
        with private_file_lock(lock_path, timeout=CONTEXT_LOCK_TIMEOUT_SECONDS):
            manifest = _read_manifest(manifest_path)
            retained = [
                source for source in manifest.sources if source.source_id != record.source_id
            ]
            if len(retained) >= MAX_STORED_CONTEXT_SOURCES:
                raise ContextSourceError(
                    "The context catalog is full; remove an attachment before adding another."
                )
            retained.append(record)
            _write_manifest(
                manifest_path,
                _ContextSourceManifest(
                    sources=sorted(
                        retained,
                        key=lambda source: (
                            source.kind.value,
                            source.label.casefold(),
                            source.source_id,
                        ),
                    )
                ),
            )
    except FileLockTimeout as exc:
        raise ContextSourceError("Context sources are busy; try again.") from exc
    except AtomicPersistenceError as exc:
        raise ContextSourceError("Context source changes could not be saved safely.") from exc
    return load_context_catalog(
        project_root,
        focus_source_id=record.source_id,
    )


def remove_context_source(project_root: Path, source_id: str) -> ContextCatalog:
    """Remove one source by its opaque identifier without touching source files."""

    if len(source_id) != _SOURCE_ID_LENGTH or any(
        character not in "0123456789abcdef" for character in source_id
    ):
        raise ContextSourceError("Context source identifier is invalid.")
    manifest_path, lock_path = _storage_paths(project_root)
    try:
        with private_file_lock(lock_path, timeout=CONTEXT_LOCK_TIMEOUT_SECONDS):
            manifest = _read_manifest(manifest_path)
            retained = [source for source in manifest.sources if source.source_id != source_id]
            if len(retained) == len(manifest.sources):
                raise ContextSourceError("Context source no longer exists.")
            _write_manifest(
                manifest_path,
                _ContextSourceManifest(sources=retained),
            )
    except FileLockTimeout as exc:
        raise ContextSourceError("Context sources are busy; try again.") from exc
    except AtomicPersistenceError as exc:
        raise ContextSourceError("Context source changes could not be saved safely.") from exc
    return load_context_catalog(project_root)


def _storage_paths(project_root: Path) -> tuple[Path, Path]:
    root = project_root.absolute()
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ContextSourceError("The dashboard workspace is unavailable.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ContextSourceError("The dashboard workspace is unavailable.")
    storage = root / _MANIFEST_DIRECTORY
    return storage / _MANIFEST_NAME, storage / _LOCK_NAME


def _read_manifest(path: Path) -> _ContextSourceManifest:
    try:
        with open_binary_read_no_follow(path, private=True) as stream:
            raw = stream.read(CONTEXT_MANIFEST_MAX_BYTES + 1)
    except FileNotFoundError:
        return _ContextSourceManifest()
    except (AtomicPersistenceError, OSError) as exc:
        raise ContextSourceError("Context sources could not be read safely.") from exc
    if len(raw) > CONTEXT_MANIFEST_MAX_BYTES:
        raise ContextSourceError("Context source metadata exceeds its byte limit.")
    try:
        value = json.loads(raw.decode("utf-8"))
        return _ContextSourceManifest.model_validate(value)
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValidationError,
    ) as exc:
        raise ContextSourceError("Context source metadata is invalid.") from exc


def _write_manifest(path: Path, manifest: _ContextSourceManifest) -> None:
    value = manifest.model_dump(mode="json", by_alias=True)
    try:
        byte_count = len(f"{json.dumps(value, indent=2, ensure_ascii=False)}\n".encode())
    except (RecursionError, TypeError, ValueError) as exc:
        raise ContextSourceError("Context source metadata is invalid.") from exc
    if byte_count > CONTEXT_MANIFEST_MAX_BYTES:
        raise ContextSourceError(
            "Context source metadata reached its local capacity; "
            "remove an attachment or choose a narrower source."
        )
    write_json_atomic(path, value)


def _catalog(
    manifest: _ContextSourceManifest,
    *,
    focus_source_id: str | None = None,
) -> ContextCatalog:
    summaries = tuple(
        ContextSourceSummary(
            source_id=source.source_id,
            kind=source.kind,
            label=source.label,
            indexed_file_count=len(source.files),
            partial=source.partial,
            available=_source_available(source),
        )
        for source in manifest.sources
    )
    partial_count = sum(source.partial for source in summaries)
    missing_count = sum(not source.available for source in summaries)
    messages: list[str] = []
    if partial_count:
        messages.append(
            f"{partial_count} large "
            f"{'source was' if partial_count == 1 else 'sources were'} "
            "partially indexed; choose a narrower folder for complete coverage."
        )
    if missing_count:
        messages.append(
            f"{missing_count} attached "
            f"{'source is' if missing_count == 1 else 'sources are'} unavailable."
        )
    return ContextCatalog(
        sources=summaries,
        notice=" ".join(messages),
        focus_source_id=focus_source_id,
    )


def _selected_path(project_root: Path, raw_path: str) -> Path:
    rendered = raw_path.strip()
    if not rendered or len(rendered) > MAX_SOURCE_PATH_LENGTH:
        raise ContextSourceError("Choose a valid local file or folder.")
    try:
        candidate = Path(rendered).expanduser()
        selected = (
            candidate.absolute()
            if candidate.is_absolute()
            else (project_root.absolute() / candidate).absolute()
        )
    except (OSError, RuntimeError) as exc:
        raise ContextSourceError("Choose a valid local file or folder.") from exc
    if selected == Path(selected.anchor):
        raise ContextSourceError("Choose a project file or folder, not a filesystem root.")
    _reject_symlinked_source(selected)
    return selected


def _reject_symlinked_source(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise ContextSourceError("The selected context source is unavailable.") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ContextSourceError("Symbolic links cannot be attached as context sources.")


def _inventory(source: Path) -> _SourceInventory:
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise ContextSourceError("The selected context source is unavailable.") from exc
    if stat.S_ISREG(metadata.st_mode):
        if not _supported_file(source.name):
            raise ContextSourceError("Choose a supported text source file.")
        return _SourceInventory(
            is_directory=False,
            files=(source.name,),
            partial=False,
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise ContextSourceError("Choose a regular file or directory.")
    return _directory_inventory(source)


def _directory_inventory(source: Path) -> _SourceInventory:
    pending: deque[tuple[Path, PurePosixPath]] = deque([(source, PurePosixPath())])
    files: list[str] = []
    inspected = 0
    while pending:
        directory, prefix = pending.popleft()
        for entry in _directory_entries(directory):
            inspected += 1
            if inspected > MAX_DISCOVERED_ENTRIES:
                return _completed_inventory(files, partial=True)
            relative = prefix / entry.name
            rendered = relative.as_posix()
            if len(rendered) > MAX_RELATIVE_PATH_LENGTH:
                continue
            if entry.is_directory:
                if entry.name.casefold() not in _EXCLUDED_DIRECTORIES:
                    pending.append((directory / entry.name, relative))
                continue
            if not entry.is_file or not _supported_file(entry.name):
                continue
            if len(files) >= MAX_INDEXED_FILES_PER_SOURCE:
                return _completed_inventory(files, partial=True)
            files.append(rendered)
    return _completed_inventory(files, partial=False)


def _completed_inventory(
    files: list[str],
    *,
    partial: bool,
) -> _SourceInventory:
    if not files:
        raise ContextSourceError("The selected location contains no supported context files.")
    return _SourceInventory(
        is_directory=True,
        files=tuple(files),
        partial=partial,
    )


def _directory_entries(directory: Path) -> tuple[_ScannedEntry, ...]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(directory, flags)
        try:
            with os.scandir(descriptor) as scanner:
                entries = tuple(
                    _ScannedEntry(
                        name=entry.name,
                        is_directory=entry.is_dir(follow_symlinks=False),
                        is_file=entry.is_file(follow_symlinks=False),
                        is_symlink=entry.is_symlink(),
                    )
                    for entry in scanner
                )
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ContextSourceError(
            "A selected context directory could not be indexed safely."
        ) from exc
    return tuple(
        sorted(
            (entry for entry in entries if not entry.is_symlink),
            key=lambda entry: (entry.name.casefold(), entry.name),
        )
    )


def _supported_file(name: str) -> bool:
    folded = name.casefold()
    if (
        folded in _EXCLUDED_FILES
        or folded.startswith(".env.")
        or Path(folded).suffix in _EXCLUDED_SUFFIXES
    ):
        return False
    path = Path(folded)
    return (
        path.suffix in _TEXT_SUFFIXES or folded in _TEXT_FILENAMES or path.stem in _TEXT_FILENAMES
    )


def _source_id(kind: ContextSourceKind, path: Path) -> str:
    identity = f"{kind.value}\0{path}".encode()
    return hashlib.sha256(identity).hexdigest()


def _source_label(path: Path) -> str:
    label = path.name or "Selected source"
    if len(label) <= MAX_SOURCE_LABEL_LENGTH:
        return label
    return f"{label[: MAX_SOURCE_LABEL_LENGTH - 1]}…"


def _source_available(source: _ContextSourceRecord) -> bool:
    path = Path(source.path)
    try:
        _reject_symlinked_source(path)
        metadata = path.lstat()
    except (ContextSourceError, OSError):
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return False
    expected = stat.S_ISDIR if source.is_directory else stat.S_ISREG
    return expected(metadata.st_mode)


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "ContextCatalog",
    "ContextSourceDescriptor",
    "ContextSourceError",
    "ContextSourceKind",
    "ContextSourceSummary",
    "add_context_source",
    "load_context_catalog",
    "load_context_descriptors",
    "remove_context_source",
]
