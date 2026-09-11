"""Fail-closed durable persistence shared by framework subsystems."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, cast

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from plantain.errors import (
    AtomicCommitUncertainError,
    AtomicPersistenceError,
    AtomicTargetExistsError,
)

COPY_BUFFER_BYTES = 1_048_576
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SOURCE_DIRECTORY_MODE = 0o755
SOURCE_FILE_MODE = 0o644


def _require_supported_runtime_platform() -> None:
    if sys.platform not in {"linux", "darwin"}:
        raise AtomicPersistenceError(
            "Private runtime persistence requires Linux, macOS, or Windows through WSL2 "
            "or a Linux container"
        )


def ensure_private_directory(path: Path) -> None:
    """Create or repair one framework-owned directory with owner-only access."""

    _require_supported_runtime_platform()
    absolute = path.absolute()
    _reject_symlinked_components(absolute)
    missing: list[Path] = []
    candidate = absolute
    while not candidate.exists():
        missing.append(candidate)
        parent = candidate.parent
        if parent == candidate:
            raise AtomicPersistenceError("Private runtime directory has no usable parent")
        candidate = parent
    if not missing:
        _harden_private_path(absolute, directory=True)
        return
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            pass
        except OSError as exc:
            raise AtomicPersistenceError("Private runtime directory could not be created") from exc
        _harden_private_path(directory, directory=True)


def ensure_private_file(path: Path, *, missing_ok: bool = False) -> None:
    """Repair and verify a framework-owned regular file with owner-only access."""

    _require_supported_runtime_platform()
    absolute = path.absolute()
    _reject_symlinked_components(absolute)
    try:
        _harden_private_path(absolute, directory=False)
    except FileNotFoundError:
        if not missing_ok:
            raise AtomicPersistenceError("Private runtime file does not exist") from None


@contextmanager
def private_file_lock(path: Path, *, timeout: float = -1.0) -> Iterator[None]:
    """Acquire a verified owner-only native lock without soft-lock downgrade."""

    absolute = path.absolute()
    ensure_private_directory(absolute.parent)
    ensure_private_file(absolute, missing_ok=True)
    lock = FileLock(
        absolute,
        timeout=timeout,
        mode=PRIVATE_FILE_MODE,
        fallback_to_soft=False,
        preserve_lock_file=True,
        on_acquired=_verify_private_descriptor,
    )
    try:
        lock.acquire()
    except FileLockTimeout:
        raise
    except OSError as exc:
        raise AtomicPersistenceError("Private runtime lock could not be acquired") from exc
    try:
        ensure_private_file(absolute)
        yield
    finally:
        try:
            lock.release()
        except OSError as exc:
            raise AtomicPersistenceError("Private runtime lock could not be released") from exc


def open_private_binary_exclusive(path: Path) -> BinaryIO:
    """Create a new owner-only binary evidence file without following links."""

    absolute = path.absolute()
    ensure_private_directory(absolute.parent)
    ensure_private_file(absolute, missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(absolute, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise AtomicPersistenceError("Private runtime file could not be created") from exc
    try:
        _verify_private_descriptor(descriptor)
    except AtomicPersistenceError:
        try:
            os.close(descriptor)
        finally:
            absolute.unlink(missing_ok=True)
        raise
    return cast("BinaryIO", os.fdopen(descriptor, "wb"))


@contextmanager
def open_binary_read_no_follow(
    path: Path,
    *,
    private: bool = False,
) -> Iterator[BinaryIO]:
    """Open one regular file through descriptor-relative, no-follow traversal."""

    descriptor = _open_read_descriptor(path)
    try:
        if private:
            _harden_private_descriptor(descriptor)
        else:
            _regular_descriptor_metadata(descriptor)
        handle = cast("BinaryIO", os.fdopen(descriptor, "rb"))
        descriptor = -1
        with handle:
            yield handle
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise AtomicPersistenceError("Runtime file descriptor could not be closed") from exc


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    """Stream JSON to a private sibling file and durably replace the target."""

    descriptor, temporary = _create_sibling(path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _commit(temporary, path)
    except Exception as exc:  # noqa: BLE001 - every failure must clean the private stage.
        _raise_after_cleanup(
            descriptor,
            temporary,
            exc,
            "Atomic JSON persistence failed",
        )


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Write bytes to a private sibling file and durably replace the target."""

    descriptor, temporary = _create_sibling(path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _commit(temporary, path)
    except Exception as exc:  # noqa: BLE001 - every failure must clean the private stage.
        _raise_after_cleanup(
            descriptor,
            temporary,
            exc,
            "Atomic byte persistence failed",
        )


def write_bytes_atomic_new(path: Path, payload: bytes) -> None:
    """Atomically create a private file without replacing an existing target."""

    _write_bytes_atomic_new(path, payload, private=True)


def write_source_bytes_atomic_new(path: Path, payload: bytes) -> None:
    """Atomically create a source file without changing existing directory permissions."""

    _write_bytes_atomic_new(path, payload, private=False)


def _write_bytes_atomic_new(
    path: Path,
    payload: bytes,
    *,
    private: bool,
) -> None:
    descriptor, temporary = _create_sibling(
        path,
        inspect_target=False,
        private_parent=private,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            if not private:
                _prepare_source_descriptor(handle.fileno())
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _commit_new(temporary, path, private_target=private)
    except AtomicTargetExistsError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise AtomicPersistenceError("Atomic create collision cleanup failed") from exc
        raise
    except Exception as exc:  # noqa: BLE001 - every failure must clean the private stage.
        _raise_after_cleanup(
            descriptor,
            temporary,
            exc,
            "Atomic create persistence failed",
        )


def copy_atomic(source: Path, destination: Path) -> None:
    """Copy incrementally through a private sibling file, then replace atomically."""

    descriptor, temporary = _create_sibling(destination)
    try:
        with (
            open_binary_read_no_follow(source) as input_handle,
            os.fdopen(
                descriptor,
                "wb",
            ) as output_handle,
        ):
            descriptor = -1
            shutil.copyfileobj(
                input_handle,
                output_handle,
                length=COPY_BUFFER_BYTES,
            )
            output_handle.flush()
            os.fsync(output_handle.fileno())
        _commit(temporary, destination)
    except Exception as exc:  # noqa: BLE001 - every failure must clean the private stage.
        _raise_after_cleanup(
            descriptor,
            temporary,
            exc,
            "Atomic file copy failed",
        )


def unlink_durable(path: Path, *, missing_ok: bool = False) -> None:
    """Delete a file and durably synchronize its parent directory."""

    try:
        path.unlink(missing_ok=missing_ok)
        _sync_directory(path.parent)
    except OSError as exc:
        raise AtomicPersistenceError("Durable file deletion failed") from exc


def adopt_private_file(source: Path, target: Path) -> None:
    """Atomically adopt one completed private staging file at its final path."""

    ensure_private_file(source)
    ensure_private_directory(target.parent)
    ensure_private_file(target, missing_ok=True)
    try:
        _commit(source.absolute(), target.absolute())
    except AtomicCommitUncertainError:
        raise
    except OSError as exc:
        raise AtomicPersistenceError("Private file adoption failed") from exc


def unlink_private_durable(path: Path, *, missing_ok: bool = False) -> None:
    """Durably delete one verified owner-only regular file."""

    absolute = path.absolute()
    ensure_private_file(absolute, missing_ok=missing_ok)
    unlink_durable(absolute, missing_ok=missing_ok)


def digest_file(path: Path) -> str:
    """Hash a file incrementally without retaining its contents in memory."""

    digest = hashlib.sha256()
    try:
        with open_binary_read_no_follow(path) as handle:
            while block := handle.read(COPY_BUFFER_BYTES):
                digest.update(block)
    except (OSError, AtomicPersistenceError) as exc:
        raise AtomicPersistenceError("File integrity verification failed") from exc
    return digest.hexdigest()


def _create_sibling(
    path: Path,
    *,
    inspect_target: bool = True,
    private_parent: bool = True,
) -> tuple[int, Path]:
    try:
        if private_parent:
            ensure_private_directory(path.parent)
        else:
            _ensure_source_directory(path.parent)
        if inspect_target:
            ensure_private_file(path, missing_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
    except OSError as exc:
        raise AtomicPersistenceError("Atomic persistence staging could not be created") from exc
    temporary = Path(temporary_name)
    try:
        _harden_private_path(temporary, directory=False)
    except AtomicPersistenceError:
        try:
            os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        raise
    return descriptor, temporary


def _commit(temporary: Path, target: Path) -> None:
    os.replace(temporary, target)  # noqa: PTH105 - the atomic contract requires replace.
    try:
        ensure_private_file(target)
        _sync_directory(target.parent)
    except (OSError, AtomicPersistenceError) as exc:
        raise AtomicCommitUncertainError(
            "Atomic replacement completed but private durability is unconfirmed"
        ) from exc


def _commit_new(
    temporary: Path,
    target: Path,
    *,
    private_target: bool,
) -> None:
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise AtomicTargetExistsError("Atomic create target already exists") from exc
    except OSError as exc:
        raise AtomicPersistenceError("Atomic create could not publish its target") from exc
    try:
        if private_target:
            ensure_private_file(target)
        else:
            _verify_source_path(target)
        _sync_directory(target.parent)
        temporary.unlink()
        _sync_directory(target.parent)
    except (OSError, AtomicPersistenceError) as exc:
        raise AtomicCommitUncertainError(
            "Atomic creation completed but private durability is unconfirmed"
        ) from exc


def _ensure_source_directory(path: Path) -> None:
    _require_supported_runtime_platform()
    absolute = path.absolute()
    _reject_symlinked_components(absolute)
    missing: list[Path] = []
    candidate = absolute
    while not candidate.exists():
        missing.append(candidate)
        parent = candidate.parent
        if parent == candidate:
            raise AtomicPersistenceError("Source directory has no usable parent")
        candidate = parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=SOURCE_DIRECTORY_MODE)
        except FileExistsError:
            pass
        except OSError as exc:
            raise AtomicPersistenceError("Source directory could not be created") from exc
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise AtomicPersistenceError("Source directory could not be inspected") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise AtomicPersistenceError("Source directory has an unsafe file type")


def _reject_symlinked_components(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AtomicPersistenceError("Private runtime path could not be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise AtomicPersistenceError("Private runtime path contains a symbolic link")


def _harden_private_path(path: Path, *, directory: bool) -> None:
    metadata = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise AtomicPersistenceError("Private runtime path has an unsafe file type")
    if not _is_owned_by_current_user(metadata):
        raise AtomicPersistenceError("Private runtime path is not owned by the current user")
    mode = PRIVATE_DIRECTORY_MODE if directory else PRIVATE_FILE_MODE
    try:
        path.chmod(mode, follow_symlinks=False)
        hardened = path.lstat()
    except OSError as exc:
        raise AtomicPersistenceError("Private runtime permissions could not be applied") from exc
    if stat.S_IMODE(hardened.st_mode) != mode:
        raise AtomicPersistenceError("Private runtime permissions could not be verified")


def _is_owned_by_current_user(metadata: os.stat_result) -> bool:
    return not hasattr(os, "geteuid") or metadata.st_uid == os.geteuid()


def _open_read_descriptor(path: Path) -> int:
    _require_supported_runtime_platform()
    absolute = path.absolute()
    components = absolute.parts
    if absolute == Path(absolute.anchor):
        raise AtomicPersistenceError("Runtime read path does not identify a file")
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | close_on_exec
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | close_on_exec
    directory = _open_no_follow(absolute.anchor, directory_flags)
    try:
        for component in components[1:-1]:
            next_directory = _open_no_follow(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        return _open_no_follow(components[-1], file_flags, dir_fd=directory)
    finally:
        os.close(directory)


def _open_no_follow(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
    try:
        if dir_fd is None:
            return os.open(path, flags)
        return os.open(path, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AtomicPersistenceError("Runtime file could not be opened safely") from exc


def _regular_descriptor_metadata(descriptor: int) -> os.stat_result:
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise AtomicPersistenceError("Runtime file could not be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AtomicPersistenceError("Runtime path has an unsafe file type")
    return metadata


def _harden_private_descriptor(descriptor: int) -> None:
    metadata = _regular_descriptor_metadata(descriptor)
    if not _is_owned_by_current_user(metadata):
        raise AtomicPersistenceError("Private runtime path is not owned by the current user")
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise AtomicPersistenceError("Private runtime permissions could not be applied") from exc
    _verify_private_descriptor(descriptor)


def _prepare_source_descriptor(descriptor: int) -> None:
    metadata = _regular_descriptor_metadata(descriptor)
    if not _is_owned_by_current_user(metadata):
        raise AtomicPersistenceError("Source file is not owned by the current user")
    try:
        os.fchmod(descriptor, SOURCE_FILE_MODE)
    except OSError as exc:
        raise AtomicPersistenceError("Source file permissions could not be applied") from exc
    hardened = _regular_descriptor_metadata(descriptor)
    if stat.S_IMODE(hardened.st_mode) != SOURCE_FILE_MODE:
        raise AtomicPersistenceError("Source file permissions could not be verified")


def _verify_source_path(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not _is_owned_by_current_user(metadata):
        raise AtomicPersistenceError("Source file has an unsafe owner or file type")
    if stat.S_IMODE(metadata.st_mode) != SOURCE_FILE_MODE:
        raise AtomicPersistenceError("Source file permissions could not be verified")


def _verify_private_descriptor(descriptor: int) -> None:
    metadata = _regular_descriptor_metadata(descriptor)
    if not _is_owned_by_current_user(metadata):
        raise AtomicPersistenceError("Private runtime path is not owned by the current user")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
        raise AtomicPersistenceError("Private runtime permissions could not be verified")


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _raise_after_cleanup(
    descriptor: int,
    temporary: Path,
    operation_error: Exception,
    message: str,
) -> NoReturn:
    commit_uncertain = isinstance(operation_error, AtomicCommitUncertainError)
    cleanup_errors: list[Exception] = []
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError as exc:
            cleanup_errors.append(exc)
    try:
        temporary.unlink(missing_ok=True)
    except OSError as exc:
        cleanup_errors.append(exc)
    if cleanup_errors:
        error_type = AtomicCommitUncertainError if commit_uncertain else AtomicPersistenceError
        raise error_type(f"{message}; staging cleanup also failed") from (
            ExceptionGroup(
                "atomic persistence and cleanup failures",
                [operation_error, *cleanup_errors],
            )
        )
    if commit_uncertain:
        raise AtomicCommitUncertainError(
            f"{message}; target replacement completed but directory durability is unconfirmed"
        ) from operation_error
    raise AtomicPersistenceError(message) from operation_error


__all__ = [
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "SOURCE_DIRECTORY_MODE",
    "SOURCE_FILE_MODE",
    "adopt_private_file",
    "copy_atomic",
    "digest_file",
    "ensure_private_directory",
    "ensure_private_file",
    "open_private_binary_exclusive",
    "private_file_lock",
    "unlink_durable",
    "unlink_private_durable",
    "write_bytes_atomic",
    "write_bytes_atomic_new",
    "write_json_atomic",
    "write_source_bytes_atomic_new",
]
