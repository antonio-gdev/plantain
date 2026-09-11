"""Isolated browser lifecycle, navigation, tracing, and cleanup behavior."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities import ui_browser
from plantain.activities.ui_browser import BrowserOptions, BrowserSession
from plantain.activities.ui_errors import BrowserLifecycleError
from plantain.errors import ConfigurationError
from plantain.persistence import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE

DEFAULT_TIMEOUT_MS = 1_000
NAVIGATION_TIMEOUT_MS = 2_000
VIEWPORT_WIDTH = 1_280
VIEWPORT_HEIGHT = 720
TRACE_RETENTION_CASES = [
    ((1, 2, 100), {"newest", "middle"}),
    ((1, 3, 5), {"newest"}),
]


class FakeTelemetry:
    def __init__(self) -> None:
        self.attached: list[object] = []
        self.close_calls = 0

    def attach(self, context: object) -> None:
        self.attached.append(context)

    def close(self) -> None:
        self.close_calls += 1


class FakeProvisioner:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def ensure_available(self, browser: str, executable: str) -> None:
        self.calls.append((browser, executable))
        if self.error is not None:
            raise self.error


@dataclass
class FakePage:
    url: str = "about:blank"
    final_url: str | None = None
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = field(default_factory=list)

    async def goto(self, value: str, **options: object) -> None:
        self.calls.append(("goto", (value,), options))
        self.url = self.final_url or value

    def set_default_timeout(self, value: int) -> None:
        self.calls.append(("set_default_timeout", (value,), {}))


class FakeTracing:
    def __init__(self, *, stop_error: Exception | None = None) -> None:
        self.stop_error = stop_error
        self.start_calls: list[dict[str, object]] = []
        self.stop_calls: list[dict[str, object]] = []

    async def start(self, **options: object) -> None:
        self.start_calls.append(options)

    async def stop(self, **options: object) -> None:
        self.stop_calls.append(options)
        if self.stop_error is not None:
            raise self.stop_error
        path = options.get("path")
        if isinstance(path, Path):
            path.write_bytes(b"synthetic trace")


class FakeContext:
    def __init__(
        self,
        page: FakePage,
        events: list[str],
        *,
        tracing: FakeTracing | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.page = page
        self.events = events
        self.tracing = tracing or FakeTracing()
        self.close_error = close_error
        self.default_timeout: int | None = None
        self.navigation_timeout: int | None = None
        self.routes: list[tuple[str, object]] = []
        self.websocket_routes: list[tuple[str, object]] = []
        self.listeners: list[tuple[str, object]] = []
        self.expected_events: list[tuple[str, Any, float | None, object]] = []

    def set_default_timeout(self, value: int) -> None:
        self.default_timeout = value

    def set_default_navigation_timeout(self, value: int) -> None:
        self.navigation_timeout = value

    async def route(self, pattern: str, handler: object) -> None:
        self.routes.append((pattern, handler))

    async def route_web_socket(self, pattern: str, handler: object) -> None:
        self.websocket_routes.append((pattern, handler))

    def on(self, event: str, handler: object) -> None:
        self.listeners.append((event, handler))

    def expect_event(
        self,
        event: str,
        predicate: Any = None,
        *,
        timeout: float | None = None,
    ) -> object:
        waiter = object()
        self.expected_events.append((event, predicate, timeout, waiter))
        return waiter

    async def new_page(self) -> FakePage:
        self.events.append("new_page")
        return self.page

    async def close(self) -> None:
        self.events.append("context.close")
        if self.close_error is not None:
            raise self.close_error


class FakeBrowser:
    def __init__(
        self,
        context: FakeContext,
        events: list[str],
        *,
        close_error: Exception | None = None,
    ) -> None:
        self.context = context
        self.events = events
        self.close_error = close_error
        self.context_options: list[dict[str, object]] = []

    async def new_context(self, **options: object) -> FakeContext:
        self.events.append("new_context")
        self.context_options.append(options)
        return self.context

    async def close(self) -> None:
        self.events.append("browser.close")
        if self.close_error is not None:
            raise self.close_error


class FakeBrowserType:
    executable_path = "/synthetic/chromium"

    def __init__(
        self,
        browser: FakeBrowser | None,
        events: list[str],
        *,
        launch_error: Exception | None = None,
    ) -> None:
        self.browser = browser
        self.events = events
        self.launch_error = launch_error
        self.launch_calls: list[dict[str, object]] = []

    async def launch(self, **options: object) -> FakeBrowser:
        self.events.append("launch")
        self.launch_calls.append(options)
        if self.launch_error is not None:
            raise self.launch_error
        assert self.browser is not None
        return self.browser


class FakePlaywright:
    def __init__(
        self,
        browser_type: FakeBrowserType,
        events: list[str],
        *,
        stop_error: Exception | None = None,
    ) -> None:
        self.chromium = browser_type
        self.firefox = browser_type
        self.webkit = browser_type
        self.events = events
        self.stop_error = stop_error

    async def stop(self) -> None:
        self.events.append("playwright.stop")
        if self.stop_error is not None:
            raise self.stop_error


class FakePlaywrightManager:
    def __init__(
        self,
        driver: FakePlaywright | None,
        *,
        start_error: Exception | None = None,
    ) -> None:
        self.driver = driver
        self.start_error = start_error
        self.start_calls = 0

    async def start(self) -> FakePlaywright:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        assert self.driver is not None
        return self.driver


@dataclass
class BrowserStack:
    session: BrowserSession
    telemetry: FakeTelemetry
    provisioner: FakeProvisioner
    manager: FakePlaywrightManager
    driver: FakePlaywright
    browser_type: FakeBrowserType
    browser: FakeBrowser
    context: FakeContext
    page: FakePage
    events: list[str]


async def _allow(url: str) -> str:
    return url


def _stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trace_mode: str = "off",
    storage_state: Path | None = None,
    trace_limits: tuple[int, int, int] = (7, 20, 1_073_741_824),
) -> BrowserStack:
    events: list[str] = []
    page = FakePage()
    context = FakeContext(page, events)
    browser = FakeBrowser(context, events)
    browser_type = FakeBrowserType(browser, events)
    driver = FakePlaywright(browser_type, events)
    manager = FakePlaywrightManager(driver)
    telemetry = FakeTelemetry()
    provisioner = FakeProvisioner()
    retention_days, max_archives, max_bytes = trace_limits
    monkeypatch.setattr(ui_browser, "async_playwright", lambda: manager)
    options = BrowserOptions(
        timeout_ms=DEFAULT_TIMEOUT_MS,
        navigation_timeout_ms=NAVIGATION_TIMEOUT_MS,
        viewport_width=VIEWPORT_WIDTH,
        viewport_height=VIEWPORT_HEIGHT,
        storage_state=storage_state,
        trace_mode=cast("Any", trace_mode),
        trace_dir=tmp_path / "traces",
        trace_data_governance_approved=True,
        trace_storage_encrypted=True,
        trace_retention_days=retention_days,
        trace_max_archives=max_archives,
        trace_max_bytes=max_bytes,
        auto_install=False,
    )
    session = BrowserSession(
        options,
        _allow,
        _allow,
        cast("Any", telemetry),
        cast("Any", provisioner),
    )
    return BrowserStack(
        session=session,
        telemetry=telemetry,
        provisioner=provisioner,
        manager=manager,
        driver=driver,
        browser_type=browser_type,
        browser=browser,
        context=context,
        page=page,
        events=events,
    )


def _seed_trace_retention_files(
    trace_dir: Path,
) -> tuple[dict[str, Path], Path, Path]:
    trace_dir.mkdir()
    now = time.time()
    archives = {
        "newest": trace_dir / f"trace_{'1' * 32}.zip",
        "middle": trace_dir / f"trace_{'2' * 32}.zip",
        "expired": trace_dir / f"trace_{'3' * 32}.zip",
    }
    for path in archives.values():
        path.write_bytes(b"data")
    os.utime(archives["newest"], (now, now))
    os.utime(archives["middle"], (now - 1, now - 1))
    os.utime(archives["expired"], (1, 1))
    partial = trace_dir / f".trace_{'4' * 32}.partial"
    partial.write_bytes(b"data")
    os.utime(partial, (1, 1))
    unrelated = trace_dir / "trace_notes.zip"
    unrelated.write_bytes(b"keep")
    return archives, partial, unrelated


def test_page_requires_started_session() -> None:
    session = BrowserSession(
        BrowserOptions(),
        _allow,
        _allow,
        cast("Any", FakeTelemetry()),
        cast("Any", FakeProvisioner()),
    )

    with pytest.raises(BrowserLifecycleError, match="has not been started"):
        _ = session.page


def test_response_expectation_uses_isolated_browser_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _stack(tmp_path, monkeypatch)
    asyncio.run(stack.session.start())
    waiter = stack.session.expect_response(
        url_pattern="**/api/orders",
        method="POST",
        statuses=(200, 201),
        timeout_ms=DEFAULT_TIMEOUT_MS,
    )

    event, predicate, timeout, expected_waiter = stack.context.expected_events[0]
    response = SimpleNamespace(
        url="https://example.test/api/orders",
        request=SimpleNamespace(method="POST"),
        status=201,
    )
    assert event == "response"
    assert cast("Any", predicate)(response) is True
    response.status = 500
    assert cast("Any", predicate)(response) is False
    assert timeout == DEFAULT_TIMEOUT_MS
    assert waiter is expected_waiter
    asyncio.run(stack.session.close())


def test_start_builds_one_hardened_context_and_reuses_active_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_state = tmp_path / "state.json"
    stack = _stack(
        tmp_path,
        monkeypatch,
        trace_mode="on",
        storage_state=storage_state,
    )

    async def exercise() -> tuple[FakePage, FakePage]:
        first = await stack.session.start()
        second = await stack.session.start()
        return cast("FakePage", first), cast("FakePage", second)

    first, second = asyncio.run(exercise())

    assert first is stack.page
    assert second is first
    assert stack.session.page is stack.page
    assert stack.manager.start_calls == 1
    assert stack.provisioner.calls == [("chromium", "/synthetic/chromium")]
    assert stack.browser_type.launch_calls == [{"headless": True, "slow_mo": 0}]
    assert stack.browser.context_options == [
        {
            "accept_downloads": False,
            "ignore_https_errors": False,
            "locale": "en-US",
            "service_workers": "block",
            "timezone_id": "UTC",
            "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "storage_state": str(storage_state),
        }
    ]
    assert stack.context.default_timeout == DEFAULT_TIMEOUT_MS
    assert stack.context.navigation_timeout == NAVIGATION_TIMEOUT_MS
    assert stack.telemetry.attached == [stack.context]
    assert stack.context.tracing.start_calls == [
        {"screenshots": True, "snapshots": True, "sources": False}
    ]
    assert stack.context.routes[0][0] == "**/*"
    assert stack.context.websocket_routes[0][0] == "**/*"
    assert stack.context.listeners[0][0] == "page"
    assert stack.events[:3] == ["launch", "new_context", "new_page"]

    asyncio.run(stack.session.close(failed=False))

    assert stack.session.trace_path is not None
    staging_path = stack.context.tracing.stop_calls[0]["path"]
    assert isinstance(staging_path, Path)
    assert staging_path != stack.session.trace_path
    assert not staging_path.exists()
    assert stack.session.trace_path.parent == tmp_path / "traces"
    if os.name != "nt":
        assert (
            stat.S_IMODE(stack.session.trace_path.parent.stat().st_mode) == PRIVATE_DIRECTORY_MODE
        )
        assert stat.S_IMODE(stack.session.trace_path.stat().st_mode) == PRIVATE_FILE_MODE
    assert stack.events[-3:] == [
        "context.close",
        "browser.close",
        "playwright.stop",
    ]
    assert stack.telemetry.close_calls == 1
    with pytest.raises(BrowserLifecycleError, match="has not been started"):
        _ = stack.session.page


@pytest.mark.parametrize(
    ("approved", "encrypted", "setting"),
    [
        (False, False, "TRACE_DATA_GOVERNANCE_APPROVED"),
        (True, False, "TRACE_STORAGE_ENCRYPTED"),
    ],
)
def test_enabled_trace_requires_explicit_governance(
    approved: bool,
    encrypted: bool,
    setting: str,
) -> None:
    with pytest.raises(ConfigurationError, match=setting):
        BrowserSession(
            BrowserOptions(
                trace_mode="on",
                trace_data_governance_approved=approved,
                trace_storage_encrypted=encrypted,
            ),
            _allow,
            _allow,
            cast("Any", FakeTelemetry()),
            cast("Any", FakeProvisioner()),
        )


@pytest.mark.parametrize(("limits", "remaining"), TRACE_RETENTION_CASES)
def test_trace_retention_is_bounded_and_ignores_unrelated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: tuple[int, int, int],
    remaining: set[str],
) -> None:
    trace_dir = tmp_path / "traces"
    archives, partial, unrelated = _seed_trace_retention_files(trace_dir)
    stack = _stack(
        tmp_path,
        monkeypatch,
        trace_mode="retain-on-failure",
        trace_limits=limits,
    )

    asyncio.run(stack.session.start())

    assert {name for name, path in archives.items() if path.exists()} == remaining
    assert not partial.exists()
    assert unrelated.read_bytes() == b"keep"
    asyncio.run(stack.session.close(failed=False))


def test_prepare_reuses_driver_and_rechecks_binary_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _stack(tmp_path, monkeypatch)

    async def exercise() -> None:
        await stack.session.prepare()
        await stack.session.prepare()

    asyncio.run(exercise())

    assert stack.manager.start_calls == 1
    assert stack.provisioner.calls == [
        ("chromium", "/synthetic/chromium"),
        ("chromium", "/synthetic/chromium"),
    ]


def test_prepare_preserves_lifecycle_errors_and_wraps_driver_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = BrowserLifecycleError("browser binary unavailable")
    stack = _stack(tmp_path, monkeypatch)
    stack.session._provisioner = cast("Any", FakeProvisioner(expected))

    with pytest.raises(BrowserLifecycleError) as preserved:
        asyncio.run(stack.session.prepare())
    assert preserved.value is expected

    telemetry = FakeTelemetry()
    manager = FakePlaywrightManager(None, start_error=RuntimeError("driver detail"))
    monkeypatch.setattr(ui_browser, "async_playwright", lambda: manager)
    session = BrowserSession(
        BrowserOptions(),
        _allow,
        _allow,
        cast("Any", telemetry),
        cast("Any", FakeProvisioner()),
    )

    with pytest.raises(BrowserLifecycleError, match="Unable to prepare") as wrapped:
        asyncio.run(session.prepare())
    assert isinstance(wrapped.value.__cause__, RuntimeError)
    assert "driver detail" not in str(wrapped.value)


def test_prepared_driver_requires_initialized_playwright() -> None:
    session = BrowserSession(
        BrowserOptions(),
        _allow,
        _allow,
        cast("Any", FakeTelemetry()),
        cast("Any", FakeProvisioner()),
    )

    with pytest.raises(BrowserLifecycleError, match="did not initialize"):
        session._prepared_driver()


def test_start_failure_cleans_partial_driver_and_returns_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _stack(tmp_path, monkeypatch)
    stack.browser_type.launch_error = RuntimeError("synthetic launch detail")

    with pytest.raises(BrowserLifecycleError, match="Unable to start") as raised:
        asyncio.run(stack.session.start())

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "synthetic launch detail" not in str(raised.value)
    assert stack.events == ["launch", "playwright.stop"]
    assert stack.telemetry.close_calls == 1


def test_navigation_validates_requested_and_final_redirect_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _stack(tmp_path, monkeypatch)
    stack.page.final_url = "https://example.test/inventory.html"
    validated: list[str] = []

    async def validate(url: str) -> str:
        validated.append(url)
        return url

    stack.session._validate_url = validate

    page = asyncio.run(
        stack.session.navigate(
            "https://example.test/login",
            wait_until="load",
        )
    )

    assert page is stack.page
    assert validated == [
        "https://example.test/login",
        "https://example.test/inventory.html",
    ]
    assert stack.page.calls == [
        (
            "goto",
            ("https://example.test/login",),
            {"wait_until": "load", "timeout": NAVIGATION_TIMEOUT_MS},
        )
    ]
    asyncio.run(stack.session.close())


def test_navigation_rejects_initial_policy_and_wraps_page_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _stack(tmp_path, monkeypatch)

    async def reject(_url: str) -> str:
        raise ConfigurationError("blocked target")

    stack.session._validate_url = reject
    with pytest.raises(ConfigurationError, match="blocked target"):
        asyncio.run(stack.session.navigate("https://blocked.example.test"))
    assert stack.manager.start_calls == 0

    stack = _stack(tmp_path, monkeypatch)

    async def fail_goto(_value: str, **_options: object) -> None:
        raise RuntimeError("navigation detail")

    stack.page.goto = fail_goto  # type: ignore[method-assign]
    with pytest.raises(BrowserLifecycleError, match="Navigation failed") as raised:
        asyncio.run(stack.session.navigate("https://example.test"))
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "navigation detail" not in str(raised.value)
    asyncio.run(stack.session.close())


@pytest.mark.parametrize(
    ("failed", "expects_path"),
    [(False, False), (True, True)],
)
def test_retain_on_failure_trace_policy(
    tmp_path: Path,
    failed: bool,
    expects_path: bool,
) -> None:
    events: list[str] = []
    tracing = FakeTracing()
    context = FakeContext(FakePage(), events, tracing=tracing)
    browser = FakeBrowser(context, events)
    browser_type = FakeBrowserType(browser, events)
    driver = FakePlaywright(browser_type, events)
    telemetry = FakeTelemetry()
    session = BrowserSession(
        BrowserOptions(
            trace_mode="retain-on-failure",
            trace_dir=tmp_path / "traces",
            trace_data_governance_approved=True,
            trace_storage_encrypted=True,
        ),
        _allow,
        _allow,
        cast("Any", telemetry),
        cast("Any", FakeProvisioner()),
    )
    session._context = cast("Any", context)
    session._browser = cast("Any", browser)
    session._playwright = cast("Any", driver)
    session._trace_started = True

    asyncio.run(session.close(failed=failed))

    assert bool(tracing.stop_calls[0]) is expects_path
    assert (session.trace_path is not None) is expects_path
    assert telemetry.close_calls == 1


def test_close_attempts_all_resources_and_aggregates_failures(tmp_path: Path) -> None:
    events: list[str] = []
    tracing = FakeTracing(stop_error=RuntimeError("trace close detail"))
    context = FakeContext(
        FakePage(),
        events,
        tracing=tracing,
        close_error=RuntimeError("context close detail"),
    )
    browser = FakeBrowser(
        context,
        events,
        close_error=RuntimeError("browser close detail"),
    )
    browser_type = FakeBrowserType(browser, events)
    driver = FakePlaywright(
        browser_type,
        events,
        stop_error=RuntimeError("driver close detail"),
    )
    telemetry = FakeTelemetry()
    session = BrowserSession(
        BrowserOptions(
            trace_mode="on",
            trace_dir=tmp_path / "traces",
            trace_data_governance_approved=True,
            trace_storage_encrypted=True,
        ),
        _allow,
        _allow,
        cast("Any", telemetry),
        cast("Any", FakeProvisioner()),
    )
    session._context = cast("Any", context)
    session._browser = cast("Any", browser)
    session._playwright = cast("Any", driver)
    session._active_page = cast("Any", FakePage())
    session._trace_started = True

    with pytest.raises(BrowserLifecycleError, match="4 failure") as raised:
        asyncio.run(session.close(failed=True))

    assert isinstance(raised.value.__cause__, ExceptionGroup)
    assert events == ["context.close", "browser.close", "playwright.stop"]
    assert telemetry.close_calls == 1
    assert session._active_page is None
    assert session._context is None
    assert session._browser is None
    assert session._playwright is None


def test_activate_track_page_and_async_context_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _stack(tmp_path, monkeypatch)
    popup = FakePage()
    stack.session.activate(cast("Any", popup))

    assert stack.session.page is popup

    tracked = FakePage()
    stack.session._track_page(cast("Any", tracked))
    assert tracked.calls == [("set_default_timeout", (DEFAULT_TIMEOUT_MS,), {})]

    stack.session._active_page = None

    async def exercise() -> BrowserSession:
        async with stack.session as entered:
            assert entered.page is stack.page
            return entered

    entered = asyncio.run(exercise())

    assert entered is stack.session
    assert stack.telemetry.close_calls == 1
    assert stack.events[-3:] == [
        "context.close",
        "browser.close",
        "playwright.stop",
    ]
