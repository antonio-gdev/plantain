"""Secure lazy Playwright browser provisioning."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from plantain.activities.ui_browser_provisioning import (
    BrowserProvisioner,
    _installer_environment,
    _lock_path,
)
from plantain.activities.ui_errors import BrowserLifecycleError
from plantain.persistence import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE

CUSTOM_HOST_NAMES = (
    "PLAYWRIGHT_DOWNLOAD_HOST",
    "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
    "PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST",
    "PLAYWRIGHT_WEBKIT_DOWNLOAD_HOST",
)


@pytest.fixture(autouse=True)
def _clear_custom_download_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CUSTOM_HOST_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _run_threads_inline_on_mounted_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_inline(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)


def test_existing_browser_skips_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "browser"
    executable.write_bytes(b"present")

    def unexpected_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("installer must not run")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    asyncio.run(BrowserProvisioner().ensure_available("chromium", str(executable)))


def test_missing_browser_respects_auto_install_opt_out(tmp_path: Path) -> None:
    with pytest.raises(BrowserLifecycleError, match="automatic installation is disabled"):
        asyncio.run(
            BrowserProvisioner(auto_install=False).ensure_available(
                "firefox",
                str(tmp_path / "missing"),
            )
        )


def test_install_is_browser_specific_shell_free_and_drops_application_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "cache" / "chromium"
    captured: dict[str, Any] = {}
    monkeypatch.setenv("APP_DB_PASSWORD", "fixture-database-password")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"installed")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    asyncio.run(BrowserProvisioner().ensure_available("chromium", str(executable)))

    assert captured["command"] == [
        str(Path(sys.executable).resolve(strict=True)),
        "-m",
        "playwright",
        "install",
        "chromium",
    ]
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    installer_environment = captured["env"]
    assert "APP_DB_PASSWORD" not in installer_environment
    assert installer_environment["XDG_CACHE_HOME"] == str(tmp_path / "xdg")
    if os.name != "nt":
        assert stat.S_IMODE(executable.parent.stat().st_mode) == PRIVATE_DIRECTORY_MODE
        lock_path = _lock_path("chromium", executable)
        assert stat.S_IMODE(lock_path.parent.stat().st_mode) == PRIVATE_DIRECTORY_MODE
        assert stat.S_IMODE(lock_path.stat().st_mode) == PRIVATE_FILE_MODE


def test_failed_install_returns_value_free_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_value = "fixture-private-installer-value"
    monkeypatch.setenv("APP_PASSWORD", protected_value)

    def fail_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(BrowserLifecycleError) as raised:
        asyncio.run(BrowserProvisioner().ensure_available("webkit", str(tmp_path / "missing")))

    assert "installation failed" in str(raised.value)
    assert protected_value not in str(raised.value)


def test_install_timeout_is_safe_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def time_out(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 1.0)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(BrowserLifecycleError, match="installation timed out"):
        asyncio.run(
            BrowserProvisioner(install_timeout_seconds=1.0).ensure_available(
                "chromium",
                str(tmp_path / "missing"),
            )
        )


def test_custom_download_host_requires_opt_in_and_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_host = "https://artifacts.example.test/playwright/"
    monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", private_host)

    with pytest.raises(BrowserLifecycleError, match="explicit framework authorization"):
        _installer_environment(False)

    environment = _installer_environment(True)
    assert environment["PLAYWRIGHT_DOWNLOAD_HOST"] == private_host.rstrip("/")

    monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", "http://artifacts.example.test")
    with pytest.raises(BrowserLifecycleError, match="must use HTTPS"):
        _installer_environment(True)
