"""Lazy, concurrency-safe provisioning for Playwright-managed browser binaries."""

from __future__ import annotations

import hashlib
import os
import subprocess  # nosec B404 - required for fixed-argv Playwright browser provisioning.
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

from filelock import Timeout as FileLockTimeout

from plantain.activities.ui_errors import BrowserLifecycleError
from plantain.errors import AtomicPersistenceError
from plantain.observability import get_logger
from plantain.persistence import ensure_private_directory, private_file_lock

if TYPE_CHECKING:
    from plantain.engine.admission import ResourceAdmission

logger = get_logger("browser.provisioning")

BrowserName = Literal["chromium", "firefox", "webkit"]

_PASSTHROUGH_ENVIRONMENT: Final = frozenset(
    {
        "APPDATA",
        "CI",
        "COMSPEC",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT",
        "PLAYWRIGHT_SKIP_BROWSER_GC",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WINDIR",
        "XDG_CACHE_HOME",
        "ALL_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_CUSTOM_DOWNLOAD_HOSTS: Final = (
    "PLAYWRIGHT_DOWNLOAD_HOST",
    "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
    "PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST",
    "PLAYWRIGHT_WEBKIT_DOWNLOAD_HOST",
)


@dataclass(frozen=True, slots=True)
class BrowserProvisioner:
    """Ensure one Playwright-pinned browser exists without shell execution."""

    auto_install: bool = True
    install_timeout_seconds: float = 300.0
    allow_custom_download_hosts: bool = False
    admission: ResourceAdmission | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    async def ensure_available(self, browser: BrowserName, executable: str) -> None:
        """Install the selected browser only when its expected binary is absent."""

        executable_path = Path(executable)
        admission = self.admission
        if admission is None:
            from plantain.engine.admission import ResourceAdmission  # noqa: PLC0415

            admission = ResourceAdmission(object())
        try:
            await admission.run_blocking(
                self._ensure_available_sync,
                browser,
                executable_path,
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            raise BrowserLifecycleError(
                "The configured Playwright browser could not be provisioned safely"
            ) from exc

    def _ensure_available_sync(
        self,
        browser: BrowserName,
        executable_path: Path,
    ) -> None:
        if executable_path.is_file():
            return
        if not self.auto_install:
            raise BrowserLifecycleError(
                "The configured Playwright browser is not installed and automatic "
                "installation is disabled"
            )
        self._install_locked(
            browser,
            executable_path,
            _installer_environment(self.allow_custom_download_hosts),
        )

    def _install_locked(
        self,
        browser: BrowserName,
        executable_path: Path,
        environment: dict[str, str],
    ) -> None:
        install_directory = executable_path.absolute().parent
        try:
            ensure_private_directory(install_directory)
        except AtomicPersistenceError as exc:
            raise BrowserLifecycleError(
                "Playwright browser installation directory is unsafe"
            ) from exc
        lock = private_file_lock(
            _lock_path(browser, executable_path),
            timeout=self.install_timeout_seconds + 30.0,
        )
        try:
            with lock:
                if executable_path.is_file():
                    return
                logger.info(
                    "Playwright browser installation started",
                    extra={"browser": browser, "provisioning_status": "running"},
                )
                completed = subprocess.run(  # noqa: S603  # nosec B603 - fixed validated argv.
                    [
                        str(Path(sys.executable).resolve(strict=True)),
                        "-m",
                        "playwright",
                        "install",
                        browser,
                    ],
                    check=False,
                    close_fds=True,
                    cwd=Path.cwd(),
                    env=environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self.install_timeout_seconds,
                )
        except FileLockTimeout as exc:
            raise BrowserLifecycleError(
                "Timed out waiting for another Playwright browser installation"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BrowserLifecycleError("Playwright browser installation timed out") from exc
        except (OSError, AtomicPersistenceError) as exc:
            raise BrowserLifecycleError("Playwright browser installation could not start") from exc

        if completed.returncode != 0 or not executable_path.is_file():
            raise BrowserLifecycleError(
                "Playwright browser installation failed; system dependencies may require "
                "separate human installation"
            )
        try:
            ensure_private_directory(install_directory)
        except AtomicPersistenceError as exc:
            raise BrowserLifecycleError(
                "Playwright browser installation directory could not be secured"
            ) from exc
        logger.info(
            "Playwright browser installation completed",
            extra={"browser": browser, "provisioning_status": "installed"},
        )


def _lock_path(browser: BrowserName, executable_path: Path) -> Path:
    identity = hashlib.sha256(f"{browser}:{executable_path.absolute()}".encode()).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"plantain-playwright-{identity}" / "install.lock"


def _installer_environment(allow_custom_download_hosts: bool) -> dict[str, str]:
    custom_values: dict[str, str] = {}
    for name in _CUSTOM_DOWNLOAD_HOSTS:
        raw_value = os.environ.get(name)
        if raw_value is None or not raw_value.strip():
            continue
        if not allow_custom_download_hosts:
            raise BrowserLifecycleError(
                "Custom Playwright download hosts require explicit framework authorization"
            )
        custom_values[name] = _validated_download_host(raw_value)

    environment: dict[str, str] = {}
    for name in _PASSTHROUGH_ENVIRONMENT:
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    environment.update(custom_values)
    return environment


def _validated_download_host(raw_value: str) -> str:
    value = raw_value.strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise BrowserLifecycleError("A custom Playwright download host is malformed") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise BrowserLifecycleError("Custom Playwright download hosts must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserLifecycleError("Custom Playwright download hosts cannot embed credentials")
    if parsed.query or parsed.fragment:
        raise BrowserLifecycleError(
            "Custom Playwright download hosts cannot contain a query or fragment"
        )
    return value.rstrip("/")


__all__ = ["BrowserName", "BrowserProvisioner"]
