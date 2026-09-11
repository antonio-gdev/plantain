"""Source-install launcher for Plantain's loopback-only Reflex dashboard."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed local Reflex command only.
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from importlib.util import find_spec
from pathlib import Path

from plantain.config import Settings
from plantain.dashboard.runtime_contract import (
    DASHBOARD_PORT_ENV,
    DEFAULT_DASHBOARD_PORT,
    PROJECT_ROOT_ENV,
    validate_dashboard_port,
)
from plantain.errors import PlantainError
from plantain.persistence import ensure_private_directory, write_bytes_atomic

DASHBOARD_ASSET_NAMES = ("plantain_favicon.png", "plantain_logo.png")
INTERRUPTED_EXIT_STATUS = 130
MAX_DASHBOARD_ASSET_BYTES = 5_242_880
MAX_PROCESS_EXIT_STATUS = 255
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SIGNAL_EXIT_BASE = 128
_RXCONFIG_SOURCE = (
    b'"""Generated entry point for Plantain\'s local dashboard."""\n\n'
    b"from plantain.dashboard.config import config\n\n"
    b'__all__ = ["config"]\n'
)


class DashboardLaunchError(PlantainError):
    """Raised when the local dashboard cannot be staged or started safely."""


@dataclass(frozen=True, slots=True)
class _DashboardRuntime:
    directory: Path
    url: str


def launch_dashboard(
    settings: Settings,
    *,
    announce: Callable[[str], None],
    port: int = DEFAULT_DASHBOARD_PORT,
) -> int:
    """Stage and run the source-installed dashboard on one loopback port."""

    selected_port = validate_dashboard_port(port)
    _require_reflex()
    runtime = _stage_runtime(settings, selected_port)
    child_environment = os.environ.copy()
    child_environment[PROJECT_ROOT_ENV] = str(settings.project_root)
    child_environment[DASHBOARD_PORT_ENV] = str(selected_port)
    announce(f"Plantain dashboard: {runtime.url}")
    announce("Press Ctrl+C to stop the local dashboard.")
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 - fixed interpreter argv.
            _reflex_command(selected_port),
            cwd=runtime.directory,
            env=child_environment,
            check=False,
        )
    except KeyboardInterrupt:
        return INTERRUPTED_EXIT_STATUS
    except OSError as exc:
        raise DashboardLaunchError("The local dashboard process could not be started") from exc
    return _process_exit_status(completed.returncode)


def _require_reflex() -> None:
    try:
        available = find_spec("reflex") is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        raise DashboardLaunchError(
            "Dashboard support is not installed; run 'uv sync --locked --extra dashboard' first"
        )


def _stage_runtime(settings: Settings, port: int) -> _DashboardRuntime:
    runtime = settings.output_dir / "dashboard" / f"port-{port}"
    assets = runtime / "assets"
    ensure_private_directory(assets)
    write_bytes_atomic(runtime / "rxconfig.py", _RXCONFIG_SOURCE)
    for asset_name in DASHBOARD_ASSET_NAMES:
        write_bytes_atomic(assets / asset_name, _packaged_asset(asset_name))
    return _DashboardRuntime(
        directory=runtime,
        url=f"http://127.0.0.1:{port}",
    )


def _packaged_asset(asset_name: str) -> bytes:
    try:
        payload = files("plantain.dashboard").joinpath("assets", asset_name).read_bytes()
    except OSError as exc:
        raise DashboardLaunchError("Packaged dashboard assets are unavailable") from exc
    if not payload.startswith(PNG_SIGNATURE) or len(payload) > MAX_DASHBOARD_ASSET_BYTES:
        raise DashboardLaunchError("A packaged dashboard asset is invalid")
    return payload


def _reflex_command(port: int) -> tuple[str, ...]:
    rendered_port = str(port)
    return (
        sys.executable,
        "-m",
        "reflex",
        "run",
        "--env",
        "prod",
        "--single-port",
        "--frontend-port",
        rendered_port,
        "--backend-port",
        rendered_port,
        "--backend-host",
        "127.0.0.1",
    )


def _process_exit_status(return_code: int) -> int:
    if return_code >= 0:
        return min(return_code, MAX_PROCESS_EXIT_STATUS)
    return min(SIGNAL_EXIT_BASE + abs(return_code), MAX_PROCESS_EXIT_STATUS)


__all__ = [
    "DASHBOARD_ASSET_NAMES",
    "DEFAULT_DASHBOARD_PORT",
    "DashboardLaunchError",
    "launch_dashboard",
]
