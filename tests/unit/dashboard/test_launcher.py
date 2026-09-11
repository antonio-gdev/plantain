"""The source-installed dashboard launcher remains local, private, and bounded."""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 - subprocess calls are replaced in tests.
import sys
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

from plantain import cli
from plantain.config import Settings
from plantain.dashboard import launcher
from plantain.dashboard.launcher import DashboardLaunchError, launch_dashboard
from plantain.dashboard.runtime_contract import (
    DASHBOARD_PORT_ENV,
    DEFAULT_DASHBOARD_PORT,
    PROJECT_ROOT_ENV,
    configured_dashboard_port,
    validate_dashboard_port,
)
from plantain.persistence import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE

CHILD_STATUS = 7
CUSTOM_PORT = 4_317
PARENT_MARKER_ENV = "PLANTAIN_TEST_PARENT_MARKER"
PARENT_MARKER_VALUE = "present"
PRIVATE_PROCESS_DETAIL = "private-process-detail"


def _settings(project_root: Path) -> Settings:
    return cast(
        "Settings",
        SimpleNamespace(
            project_root=project_root,
            output_dir=project_root / "output",
        ),
    )


def test_launcher_stages_packaged_assets_and_runs_one_loopback_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        captured.update(command=command, cwd=cwd, env=env, check=check)
        return subprocess.CompletedProcess(command, CHILD_STATUS)

    monkeypatch.setattr(launcher, "find_spec", lambda _name: object())
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setenv(PARENT_MARKER_ENV, PARENT_MARKER_VALUE)
    messages: list[str] = []

    status = launch_dashboard(
        _settings(tmp_path),
        announce=messages.append,
        port=CUSTOM_PORT,
    )

    runtime = tmp_path / "output" / "dashboard" / f"port-{CUSTOM_PORT}"
    assert status == CHILD_STATUS
    assert captured["command"] == (
        sys.executable,
        "-m",
        "reflex",
        "run",
        "--env",
        "prod",
        "--single-port",
        "--frontend-port",
        str(CUSTOM_PORT),
        "--backend-port",
        str(CUSTOM_PORT),
        "--backend-host",
        "127.0.0.1",
    )
    assert captured["cwd"] == runtime
    assert captured["check"] is False
    assert captured["env"][PROJECT_ROOT_ENV] == str(tmp_path)
    assert captured["env"][DASHBOARD_PORT_ENV] == str(CUSTOM_PORT)
    assert captured["env"][PARENT_MARKER_ENV] == PARENT_MARKER_VALUE
    assert messages == [
        f"Plantain dashboard: http://127.0.0.1:{CUSTOM_PORT}",
        "Press Ctrl+C to stop the local dashboard.",
    ]
    assert (runtime / "rxconfig.py").read_text(encoding="utf-8") == (
        '"""Generated entry point for Plantain\'s local dashboard."""\n\n'
        "from plantain.dashboard.config import config\n\n"
        '__all__ = ["config"]\n'
    )
    assert runtime.stat().st_mode & 0o777 == PRIVATE_DIRECTORY_MODE
    assert (runtime / "rxconfig.py").stat().st_mode & 0o777 == PRIVATE_FILE_MODE
    for asset_name in launcher.DASHBOARD_ASSET_NAMES:
        staged = runtime / "assets" / asset_name
        packaged = files("plantain.dashboard").joinpath("assets", asset_name).read_bytes()
        assert staged.read_bytes() == packaged
        assert staged.stat().st_mode & 0o777 == PRIVATE_FILE_MODE


@pytest.mark.parametrize(
    "value",
    [0, 65_536, True, "not-a-port", None],
)
def test_invalid_ports_are_rejected_without_reflection(value: object) -> None:
    with pytest.raises((TypeError, ValueError)) as captured:
        validate_dashboard_port(value)

    assert str(captured.value) == ("Dashboard port must be an integer between 1 and 65535")


def test_dashboard_port_environment_is_optional_and_bounded() -> None:
    assert configured_dashboard_port({}) is None
    assert configured_dashboard_port({DASHBOARD_PORT_ENV: " "}) is None
    assert configured_dashboard_port({DASHBOARD_PORT_ENV: f" {CUSTOM_PORT} "}) == CUSTOM_PORT


def test_missing_dashboard_extra_fails_before_runtime_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "find_spec", lambda _name: None)

    with pytest.raises(DashboardLaunchError, match="extra dashboard"):
        launch_dashboard(
            _settings(tmp_path),
            announce=lambda _message: None,
        )

    assert not (tmp_path / "output" / "dashboard").exists()


def test_process_start_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_process(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise OSError(PRIVATE_PROCESS_DETAIL)

    monkeypatch.setattr(launcher, "find_spec", lambda _name: object())
    monkeypatch.setattr(launcher.subprocess, "run", reject_process)

    with pytest.raises(
        DashboardLaunchError,
        match="could not be started",
    ) as captured:
        launch_dashboard(
            _settings(tmp_path),
            announce=lambda _message: None,
        )

    assert PRIVATE_PROCESS_DETAIL not in str(captured.value)


def test_keyboard_interrupt_returns_conventional_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_process(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "find_spec", lambda _name: object())
    monkeypatch.setattr(launcher.subprocess, "run", interrupt_process)

    assert (
        launch_dashboard(
            _settings(tmp_path),
            announce=lambda _message: None,
        )
        == launcher.INTERRUPTED_EXIT_STATUS
    )


def test_cli_parses_dashboard_as_a_runtime_command(tmp_path: Path) -> None:
    args = cli._parser().parse_args(
        [
            "--project-root",
            str(tmp_path),
            "--no-dotenv",
            "dashboard",
            "--port",
            str(CUSTOM_PORT),
        ]
    )

    assert args.command == "dashboard"
    assert args.port == CUSTOM_PORT
    assert args.no_dotenv is True
    assert args.project_root == tmp_path
    assert "dashboard" in cli._RUNTIME_COMMANDS
    default_args = cli._parser().parse_args(["dashboard"])
    assert default_args.port == DEFAULT_DASHBOARD_PORT


def test_cli_dispatches_to_the_dashboard_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    captured: dict[str, Any] = {}

    def fake_launch(
        supplied_settings: Settings,
        *,
        announce: Callable[[str], None],
        port: int,
    ) -> int:
        captured.update(settings=supplied_settings, port=port)
        announce("dashboard-ready")
        return CHILD_STATUS

    monkeypatch.setattr(cli, "launch_dashboard", fake_launch)

    status = cli._dashboard(
        argparse.Namespace(port=CUSTOM_PORT),
        settings,
    )

    assert status == CHILD_STATUS
    assert captured == {"settings": settings, "port": CUSTOM_PORT}
    assert capsys.readouterr().out == "dashboard-ready\n"
