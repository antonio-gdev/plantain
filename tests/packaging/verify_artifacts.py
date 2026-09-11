"""Verify the contents of Plantain wheel and source distribution artifacts."""

from __future__ import annotations

import argparse
import re
import stat
import tarfile
import zipfile
from collections.abc import Collection
from email.parser import Parser
from pathlib import Path, PurePosixPath

PROJECT_NAME = "plantain-automation"
CONSOLE_ENTRY = "plantain = plantain.cli:main"
DASHBOARD_ASSET_NAMES = ("plantain_favicon.png", "plantain_logo.png")
DASHBOARD_EXTRA = "dashboard"
MAX_DASHBOARD_ASSET_BYTES = 5_242_880
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FORBIDDEN_PARTS = frozenset(
    {
        ".agents",
        ".codex",
        ".git",
        ".playwright-driver",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "generated",
        "output",
        "snapshots",
    }
)


class ArtifactVerificationError(RuntimeError):
    """Raised when a built distribution violates its artifact contract."""


def _single_artifact(paths: list[Path], label: str) -> Path:
    if len(paths) != 1:
        raise ArtifactVerificationError(
            f"Expected exactly one {label} artifact, found {len(paths)}"
        )
    artifact = paths[0]
    if artifact.is_symlink() or not artifact.is_file():
        raise ArtifactVerificationError(f"{label} artifact must be a regular file")
    return artifact


def _validate_member_names(names: list[str], label: str) -> None:
    if len(names) != len(set(names)):
        raise ArtifactVerificationError(f"{label} contains duplicate member names")
    for name in names:
        if not name or "\\" in name:
            raise ArtifactVerificationError(f"{label} contains an invalid member path")
        path = PurePosixPath(name)
        lowered = tuple(part.casefold() for part in path.parts)
        if path.is_absolute() or ".." in path.parts:
            raise ArtifactVerificationError(f"{label} contains an unsafe member path")
        if any(part in FORBIDDEN_PARTS for part in lowered):
            raise ArtifactVerificationError(f"{label} contains forbidden runtime state")
        if any(part == ".env" or part.startswith(".env.") for part in lowered):
            raise ArtifactVerificationError(f"{label} contains an environment file")


def _require_members(names: Collection[str], required: set[str], label: str) -> None:
    missing = sorted(required.difference(names))
    if missing:
        raise ArtifactVerificationError(
            f"{label} is missing required members: {', '.join(missing)}"
        )


def _source_modules(repository: Path) -> set[str]:
    source_root = repository / "src"
    package_root = source_root / "plantain"
    return {
        path.relative_to(source_root).as_posix()
        for path in package_root.rglob("*.py")
        if path.is_file()
    }


def _dashboard_assets(repository: Path) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for asset_name in DASHBOARD_ASSET_NAMES:
        packaged = repository / "src" / "plantain" / "dashboard" / "assets" / asset_name
        source_mirror = repository / "assets" / asset_name
        for candidate in (packaged, source_mirror):
            if candidate.is_symlink() or not candidate.is_file():
                raise ArtifactVerificationError("Dashboard assets must be regular source files")
            if candidate.stat().st_size > MAX_DASHBOARD_ASSET_BYTES:
                raise ArtifactVerificationError("Dashboard asset exceeds its size limit")
        payload = packaged.read_bytes()
        if not payload.startswith(PNG_SIGNATURE):
            raise ArtifactVerificationError("Dashboard asset is not a valid PNG")
        if source_mirror.read_bytes() != payload:
            raise ArtifactVerificationError(
                "Source dashboard assets must match their packaged owners"
            )
        payloads[asset_name] = payload
    return payloads


def _verify_wheel(
    path: Path,
    required_modules: set[str],
    dashboard_assets: dict[str, bytes],
) -> None:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        _validate_member_names(names, "Wheel")
        if any(stat.S_ISLNK(member.external_attr >> 16) for member in members):
            raise ArtifactVerificationError("Wheel must not contain symbolic links")
        if any(not member.is_dir() and (member.external_attr >> 16) & 0o111 for member in members):
            raise ArtifactVerificationError("Wheel must not contain executable files")
        required_assets = {
            f"plantain/dashboard/assets/{asset_name}" for asset_name in dashboard_assets
        }
        _require_members(set(names), required_modules | required_assets, "Wheel")
        for asset_name, payload in dashboard_assets.items():
            member_name = f"plantain/dashboard/assets/{asset_name}"
            if archive.getinfo(member_name).file_size != len(payload):
                raise ArtifactVerificationError("Wheel dashboard asset has changed")
            if archive.read(member_name) != payload:
                raise ArtifactVerificationError("Wheel dashboard asset has changed")

        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        metadata_name = _single_member(metadata_names, "wheel metadata")
        entry_name = _single_member(entry_names, "wheel console entry points")
        metadata = archive.read(metadata_name).decode("utf-8")
        entry_points = archive.read(entry_name).decode("utf-8")
        if f"Name: {PROJECT_NAME}" not in metadata.splitlines():
            raise ArtifactVerificationError("Wheel metadata has the wrong project name")
        parsed_metadata = Parser().parsestr(metadata)
        provided_extras = {
            value.casefold() for value in parsed_metadata.get_all("Provides-Extra", [])
        }
        if DASHBOARD_EXTRA not in provided_extras:
            raise ArtifactVerificationError("Wheel is missing the dashboard extra")
        requirements = parsed_metadata.get_all("Requires-Dist", [])
        if not any(_is_dashboard_requirement(value) for value in requirements):
            raise ArtifactVerificationError(
                "Wheel dashboard extra is missing its Reflex dependency"
            )
        if CONSOLE_ENTRY not in entry_points.splitlines():
            raise ArtifactVerificationError("Wheel is missing the Plantain console entry point")


def _is_dashboard_requirement(requirement: str) -> bool:
    distribution, separator, marker = requirement.casefold().partition(";")
    return (
        bool(re.match(r"^reflex(?:\s|[<>=!~\[]|$)", distribution))
        and bool(separator)
        and "extra" in marker
        and DASHBOARD_EXTRA in marker
    )


def _single_member(names: list[str], label: str) -> str:
    if len(names) != 1:
        raise ArtifactVerificationError(f"Expected exactly one {label}, found {len(names)}")
    return names[0]


def _verify_sdist(
    path: Path,
    required_modules: set[str],
    dashboard_assets: dict[str, bytes],
) -> None:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        _validate_member_names(names, "Source distribution")
        if any(not (member.isfile() or member.isdir()) for member in members):
            raise ArtifactVerificationError(
                "Source distribution must contain only regular files and directories"
            )
        if any(member.isfile() and member.mode & 0o111 for member in members):
            raise ArtifactVerificationError("Source distribution must not contain executable files")
        roots = {PurePosixPath(name).parts[0] for name in names}
        root = _single_member(sorted(roots), "source distribution root")
        relative_names = {
            PurePosixPath(name).relative_to(root).as_posix()
            for name in names
            if PurePosixPath(name).as_posix() != root
        }
        required = {
            "README.md",
            "pyproject.toml",
            "rxconfig.py",
            *(f"assets/{asset_name}" for asset_name in dashboard_assets),
            *(f"src/plantain/dashboard/assets/{asset_name}" for asset_name in dashboard_assets),
            *(f"src/{module}" for module in required_modules),
        }
        _require_members(relative_names, required, "Source distribution")
        for asset_name, payload in dashboard_assets.items():
            for relative_name in (
                f"assets/{asset_name}",
                f"src/plantain/dashboard/assets/{asset_name}",
            ):
                member = archive.getmember(f"{root}/{relative_name}")
                extracted = archive.extractfile(member)
                if member.size != len(payload) or extracted is None or extracted.read() != payload:
                    raise ArtifactVerificationError(
                        "Source distribution dashboard asset has changed"
                    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    dist_dir = args.dist_dir.expanduser().resolve()
    wheel = _single_artifact(sorted(dist_dir.glob("*.whl")), "wheel")
    sdist = _single_artifact(sorted(dist_dir.glob("*.tar.gz")), "source distribution")
    required_modules = _source_modules(repository)
    dashboard_assets = _dashboard_assets(repository)
    _verify_wheel(wheel, required_modules, dashboard_assets)
    _verify_sdist(sdist, required_modules, dashboard_assets)


if __name__ == "__main__":
    main()
