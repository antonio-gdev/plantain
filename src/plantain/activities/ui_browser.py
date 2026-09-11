"""Isolated asynchronous Playwright browser lifecycle."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    WebSocketRoute,
    async_playwright,
)

from plantain.activities.ui_browser_provisioning import BrowserName, BrowserProvisioner
from plantain.activities.ui_errors import BrowserLifecycleError
from plantain.activities.ui_telemetry import NetworkTelemetryRecorder
from plantain.engine.admission import ResourceAdmission, ResourceKind, ResourcePermit
from plantain.errors import ConfigurationError
from plantain.persistence import (
    adopt_private_file,
    ensure_private_directory,
    ensure_private_file,
    private_file_lock,
    unlink_private_durable,
)

UrlValidator = Callable[[str], Awaitable[object]]
SECONDS_PER_DAY = 86_400
TRACE_RETENTION_LOCK_TIMEOUT_SECONDS = 5.0
_TRACE_ARCHIVE_NAME = re.compile(r"trace_[0-9a-f]{32}\.zip")
_TRACE_PARTIAL_NAME = re.compile(r"\.trace_[0-9a-f]{32}\.partial")


def _is_top_level_navigation(request: Request) -> bool:
    return request.is_navigation_request() and request.frame.parent_frame is None


@dataclass(frozen=True, slots=True)
class BrowserOptions:
    """Runtime browser configuration assembled from environment-backed settings."""

    browser: BrowserName = "chromium"
    headless: bool = True
    slow_mo_ms: int = 0
    timeout_ms: int = 30_000
    navigation_timeout_ms: int = 45_000
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "en-US"
    timezone_id: str = "UTC"
    ignore_https_errors: bool = False
    storage_state: Path | None = None
    trace_mode: Literal["off", "on", "retain-on-failure"] = "off"
    trace_dir: Path | None = None
    trace_data_governance_approved: bool = False
    trace_storage_encrypted: bool = False
    trace_retention_days: int = 7
    trace_max_archives: int = 20
    trace_max_bytes: int = 1_073_741_824
    auto_install: bool = True
    install_timeout_seconds: float = 300.0
    allow_custom_download_hosts: bool = False
    strict_network_policy: bool = False


def _validate_trace_options(options: BrowserOptions) -> None:
    if options.trace_mode == "off":
        return
    if not options.trace_data_governance_approved:
        raise ConfigurationError(
            "PLANTAIN_TRACE_DATA_GOVERNANCE_APPROVED=true is required when tracing is enabled"
        )
    if not options.trace_storage_encrypted:
        raise ConfigurationError(
            "PLANTAIN_TRACE_STORAGE_ENCRYPTED=true is required when tracing is enabled"
        )
    limits = (
        options.trace_retention_days,
        options.trace_max_archives,
        options.trace_max_bytes,
    )
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in limits):
        raise ConfigurationError("Trace retention limits must be positive integers")


def _trace_directory(options: BrowserOptions) -> Path:
    return options.trace_dir or Path.cwd() / "output" / "traces"


def _retained_archive(
    path: Path,
    *,
    cutoff: float,
) -> tuple[int, str, int, Path] | None:
    is_archive = _TRACE_ARCHIVE_NAME.fullmatch(path.name) is not None
    is_partial = _TRACE_PARTIAL_NAME.fullmatch(path.name) is not None
    if not is_archive and not is_partial:
        return None
    ensure_private_file(path)
    metadata = path.stat()
    if metadata.st_mtime <= cutoff:
        unlink_private_durable(path, missing_ok=True)
        return None
    if not is_archive:
        return None
    return metadata.st_mtime_ns, path.name, metadata.st_size, path


def _prune_trace_archives(
    archives: list[tuple[int, str, int, Path]],
    options: BrowserOptions,
) -> None:
    archives.sort(reverse=True)
    retained_bytes = 0
    for position, (_modified, _name, size_bytes, path) in enumerate(archives):
        exceeds_count = position >= options.trace_max_archives
        exceeds_bytes = retained_bytes + size_bytes > options.trace_max_bytes
        if exceeds_count or exceeds_bytes:
            unlink_private_durable(path, missing_ok=True)
        else:
            retained_bytes += size_bytes


def _enforce_trace_retention(options: BrowserOptions) -> None:
    trace_dir = _trace_directory(options)
    ensure_private_directory(trace_dir)
    cutoff = time.time() - (options.trace_retention_days * SECONDS_PER_DAY)
    lock_path = trace_dir / ".retention.lock"
    with private_file_lock(lock_path, timeout=TRACE_RETENTION_LOCK_TIMEOUT_SECONDS):
        archives: list[tuple[int, str, int, Path]] = []
        for candidate in trace_dir.iterdir():
            retained = _retained_archive(candidate, cutoff=cutoff)
            if retained is not None:
                archives.append(retained)
        _prune_trace_archives(archives, options)


def _response_matches(
    response: Response,
    *,
    url_pattern: str,
    method: str,
    statuses: tuple[int, ...],
) -> bool:
    return (
        fnmatchcase(response.url, url_pattern)
        and response.request.method == method
        and response.status in statuses
    )


class BrowserSession:
    """Owns one isolated browser context for a scenario execution."""

    def __init__(
        self,
        options: BrowserOptions,
        validate_url: UrlValidator,
        validate_websocket: UrlValidator,
        telemetry: NetworkTelemetryRecorder,
        provisioner: BrowserProvisioner | None = None,
        *,
        admission: ResourceAdmission | None = None,
    ) -> None:
        _validate_trace_options(options)
        self._options = options
        self._validate_url = validate_url
        self._validate_websocket = validate_websocket
        self.telemetry = telemetry
        self._admission = admission
        self._permit: ResourcePermit | None = None
        self._provisioner = provisioner or BrowserProvisioner(
            auto_install=options.auto_install,
            install_timeout_seconds=options.install_timeout_seconds,
            allow_custom_download_hosts=options.allow_custom_download_hosts,
            admission=admission,
        )
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._active_page: Page | None = None
        self._trace_started = False
        self._policy_failure: BrowserLifecycleError | None = None
        self._policy_failure_reported = False
        self.trace_path: Path | None = None

    @property
    def page(self) -> Page:
        if self._active_page is None:
            raise BrowserLifecycleError("Browser session has not been started")
        return self._active_page

    async def start(self) -> Page:
        await self.assert_policy_compliant()
        if self._active_page is not None:
            return self._active_page
        if self._admission is not None and self._permit is None:
            self._permit = await self._admission.reserve(ResourceKind.BROWSER)
        try:
            if self._options.trace_mode != "off":
                _enforce_trace_retention(self._options)
            await self.prepare()
            browser_type = getattr(self._prepared_driver(), self._options.browser)
            self._browser = await browser_type.launch(
                headless=self._options.headless,
                slow_mo=self._options.slow_mo_ms,
            )
            context_options: dict[str, object] = {
                "accept_downloads": False,
                "ignore_https_errors": self._options.ignore_https_errors,
                "locale": self._options.locale,
                "service_workers": "block",
                "timezone_id": self._options.timezone_id,
                "viewport": {
                    "width": self._options.viewport_width,
                    "height": self._options.viewport_height,
                },
            }
            if self._options.storage_state is not None:
                context_options["storage_state"] = str(self._options.storage_state)
            self._context = await self._browser.new_context(**context_options)  # type: ignore[arg-type]
            self._context.set_default_timeout(self._options.timeout_ms)
            self._context.set_default_navigation_timeout(self._options.navigation_timeout_ms)
            self.telemetry.attach(self._context)
            if self._options.trace_mode != "off":
                await self._context.tracing.start(
                    screenshots=True,
                    snapshots=True,
                    sources=False,
                )
                self._trace_started = True
            await self._context.route("**/*", self._guard_request)
            await self._context.route_web_socket("**/*", self._guard_websocket)
            self._context.on("page", self._track_page)
            active_page = await self._context.new_page()
            self._active_page = active_page
        except BrowserLifecycleError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise BrowserLifecycleError("Unable to start the isolated browser session") from exc
        return active_page

    def _prepared_driver(self) -> Playwright:
        if self._playwright is None:
            raise BrowserLifecycleError("Playwright preparation did not initialize its driver")
        return self._playwright

    async def prepare(self) -> None:
        """Start the driver and lazily provision only the configured browser binary."""

        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            browser_type = getattr(self._playwright, self._options.browser)
            await self._provisioner.ensure_available(
                self._options.browser,
                browser_type.executable_path,
            )
        except BrowserLifecycleError:
            raise
        except Exception as exc:
            raise BrowserLifecycleError("Unable to prepare the Playwright browser") from exc

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded") -> Page:
        await self.assert_policy_compliant()
        await self._validate_url(url)
        page = await self.start()
        try:
            await page.goto(
                url,
                wait_until=wait_until,  # type: ignore[arg-type]
                timeout=self._options.navigation_timeout_ms,
            )
            await self.assert_policy_compliant()
            await self._validate_url(page.url)
        except Exception as exc:
            await self.assert_policy_compliant()
            raise BrowserLifecycleError("Navigation failed or was blocked by URL policy") from exc
        return page

    def activate(self, page: Page) -> None:
        """Make an explicitly expected popup the active page."""

        self._active_page = page

    def expect_response(
        self,
        *,
        url_pattern: str,
        method: str,
        statuses: tuple[int, ...],
        timeout_ms: int,
    ) -> AbstractAsyncContextManager[Any]:
        """Arm one bounded response waiter on this scenario's browser context."""

        if self._context is None:
            raise BrowserLifecycleError("Browser session has not been started")
        return self._context.expect_event(
            "response",
            predicate=lambda response: _response_matches(
                response,
                url_pattern=url_pattern,
                method=method,
                statuses=statuses,
            ),
            timeout=timeout_ms,
        )

    async def close(self, *, failed: bool = False) -> None:
        try:
            await self._close_resources(failed=failed)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._release_permit()
            raise
        self._release_permit()

    async def _retain_trace(self) -> None:
        if self._context is None:
            raise BrowserLifecycleError("Browser trace context is unavailable")
        trace_dir = _trace_directory(self._options)
        ensure_private_directory(trace_dir)
        identifier = uuid4().hex
        staging = trace_dir / f".trace_{identifier}.partial"
        target = trace_dir / f"trace_{identifier}.zip"
        try:
            await self._context.tracing.stop(path=staging)
            ensure_private_file(staging)
            adopt_private_file(staging, target)
        except BaseException:
            unlink_private_durable(staging, missing_ok=True)
            raise
        _enforce_trace_retention(self._options)
        if not target.exists():
            raise BrowserLifecycleError("Browser trace exceeded its configured retention limit")
        self.trace_path = target

    async def _close_resources(self, *, failed: bool) -> None:
        await asyncio.sleep(0)
        policy_failure = None if self._policy_failure_reported else self._policy_failure
        failures: list[Exception] = []
        if self._context is not None and self._trace_started:
            try:
                retain = self._options.trace_mode == "on" or (
                    self._options.trace_mode == "retain-on-failure" and failed
                )
                if retain:
                    await self._retain_trace()
                else:
                    await self._context.tracing.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup must aggregate all failures.
                failures.append(exc)
            finally:
                self._trace_started = False
        for resource in (self._context, self._browser, self._playwright):
            if resource is None:
                continue
            try:
                await resource.close() if hasattr(resource, "close") else await resource.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup must aggregate all failures.
                failures.append(exc)
        self._active_page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._policy_failure = None
        self._policy_failure_reported = False
        self.telemetry.close()
        if failures:
            if policy_failure is not None:
                failures.insert(0, policy_failure)
            raise BrowserLifecycleError(
                f"Browser cleanup encountered {len(failures)} failure(s)"
            ) from ExceptionGroup("browser cleanup failures", failures)
        if policy_failure is not None:
            raise policy_failure

    def _release_permit(self) -> None:
        if self._permit is None:
            return
        self._permit.release()
        self._permit = None

    async def _guard_request(self, route: Route) -> None:
        url = route.request.url
        scheme = urlsplit(url).scheme.lower()
        if scheme in {"about", "blob", "data"}:
            await route.continue_()
            return
        try:
            await self._validate_url(url)
        except Exception:  # noqa: BLE001 - outbound policy failures must fail closed.
            if self._options.strict_network_policy or _is_top_level_navigation(route.request):
                self._latch_policy_failure("request")
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _guard_websocket(self, route: WebSocketRoute) -> None:
        try:
            await self._validate_websocket(route.url)
        except Exception:  # noqa: BLE001 - outbound policy failures must fail closed.
            if self._options.strict_network_policy:
                self._latch_policy_failure("websocket")
            self.telemetry.record_websocket_policy(route.url, allowed=False)
            await route.close(code=1008, reason="Blocked by outbound network policy")
            return
        self.telemetry.record_websocket_policy(route.url, allowed=True)
        route.connect_to_server()

    async def assert_policy_compliant(self) -> None:
        """Yield to browser callbacks, then surface a latched outbound-policy failure."""

        await asyncio.sleep(0)
        if self._policy_failure is None:
            return
        self._policy_failure_reported = True
        raise self._policy_failure

    def _latch_policy_failure(self, operation_type: str) -> None:
        if self._policy_failure is not None:
            return
        self._policy_failure = BrowserLifecycleError(
            "Browser application traffic was blocked by outbound network policy",
            safe_details={
                "failure_stage": "browser_network_policy",
                "operation_type": operation_type,
                "operation_target": "browser-network",
                "operation_error_type": "OutboundNetworkPolicyError",
            },
        )

    def _track_page(self, page: Page) -> None:
        # Popup activation is explicit in the action executor. Tracking only
        # ensures a page created by the application is closed with its context.
        page.set_default_timeout(self._options.timeout_ms)

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
