"""Browser transport policy tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.ui_browser import BrowserOptions, BrowserSession
from plantain.activities.ui_errors import BrowserLifecycleError
from plantain.activities.ui_telemetry import NetworkTelemetryRecorder
from plantain.errors import ConfigurationError
from plantain.security.secrets import SecretRegistry


class FakeWebSocketRoute:
    """Minimal WebSocket route double for policy behavior."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.connected = False
        self.closed: tuple[int | None, str | None] | None = None

    def connect_to_server(self) -> None:
        self.connected = True

    async def close(self, *, code: int | None = None, reason: str | None = None) -> None:
        self.closed = (code, reason)


class FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        navigation: bool = False,
        top_level: bool = True,
    ) -> None:
        self.url = url
        self._navigation = navigation
        self.frame = SimpleNamespace(parent_frame=None if top_level else object())

    def is_navigation_request(self) -> bool:
        return self._navigation


class FakeRoute:
    def __init__(self, url: str, *, navigation: bool = False) -> None:
        self.request = FakeRequest(url, navigation=navigation)
        self.continued = False
        self.aborted: str | None = None

    async def continue_(self) -> None:
        self.continued = True

    async def abort(self, reason: str) -> None:
        self.aborted = reason


async def _allow(_url: str) -> str:
    return _url


async def _deny(_url: str) -> str:
    raise ConfigurationError("blocked")


def test_approved_websocket_is_forwarded() -> None:
    route = FakeWebSocketRoute("wss://example.test/events")
    telemetry = NetworkTelemetryRecorder(SecretRegistry())
    session = BrowserSession(BrowserOptions(), _allow, _allow, telemetry)

    asyncio.run(session._guard_websocket(cast("Any", route)))

    assert route.connected is True
    assert route.closed is None
    assert [event["event"] for batch in telemetry.batches() for event in batch] == [
        "websocketAllowed"
    ]
    asyncio.run(session.assert_policy_compliant())
    telemetry.close()


def test_rejected_websocket_is_closed_without_forwarding() -> None:
    route = FakeWebSocketRoute("ws://127.0.0.1/events")
    telemetry = NetworkTelemetryRecorder(SecretRegistry())
    options = BrowserOptions(strict_network_policy=True)
    session = BrowserSession(options, _allow, _deny, telemetry)

    asyncio.run(session._guard_websocket(cast("Any", route)))

    assert route.connected is False
    assert route.closed == (1008, "Blocked by outbound network policy")
    assert [event["event"] for batch in telemetry.batches() for event in batch] == [
        "websocketBlocked"
    ]
    with pytest.raises(BrowserLifecycleError, match="outbound network policy"):
        asyncio.run(session.assert_policy_compliant())
    telemetry.close()


def test_standard_mode_blocks_websocket_without_failing_scenario() -> None:
    route = FakeWebSocketRoute("wss://blocked.example.test/events")
    telemetry = NetworkTelemetryRecorder(SecretRegistry())
    session = BrowserSession(BrowserOptions(), _allow, _deny, telemetry)

    asyncio.run(session._guard_websocket(cast("Any", route)))

    assert route.connected is False
    assert route.closed == (1008, "Blocked by outbound network policy")
    asyncio.run(session.assert_policy_compliant())
    telemetry.close()


def test_approved_http_request_is_forwarded() -> None:
    route = FakeRoute("https://example.test/resource")
    telemetry = NetworkTelemetryRecorder(SecretRegistry())
    session = BrowserSession(BrowserOptions(), _allow, _allow, telemetry)

    asyncio.run(session._guard_request(cast("Any", route)))

    assert route.continued is True
    assert route.aborted is None
    asyncio.run(session.assert_policy_compliant())
    telemetry.close()


def test_restricted_mode_aborts_and_latches_rejected_http_request() -> None:
    route = FakeRoute("http://127.0.0.1/resource")
    telemetry = NetworkTelemetryRecorder(SecretRegistry())
    options = BrowserOptions(strict_network_policy=True)
    session = BrowserSession(options, _deny, _allow, telemetry)

    asyncio.run(session._guard_request(cast("Any", route)))

    assert route.continued is False
    assert route.aborted == "blockedbyclient"
    with pytest.raises(BrowserLifecycleError, match="outbound network policy"):
        asyncio.run(session.assert_policy_compliant())
    telemetry.close()


def test_standard_mode_aborts_subresource_without_failing_scenario() -> None:
    route = FakeRoute("https://blocked.example.test/analytics")
    telemetry = NetworkTelemetryRecorder(SecretRegistry())
    session = BrowserSession(BrowserOptions(), _deny, _allow, telemetry)

    asyncio.run(session._guard_request(cast("Any", route)))

    assert route.continued is False
    assert route.aborted == "blockedbyclient"
    asyncio.run(session.assert_policy_compliant())
    telemetry.close()


def test_browser_internal_request_schemes_bypass_http_validator() -> None:
    calls: list[str] = []

    async def unexpected(url: str) -> str:
        calls.append(url)
        raise AssertionError("internal schemes must not reach the HTTP validator")

    telemetry = NetworkTelemetryRecorder(SecretRegistry())
    session = BrowserSession(BrowserOptions(), unexpected, _allow, telemetry)

    for target in ("about:blank", "blob:https://example.test/id", "data:text/plain,ok"):
        route = FakeRoute(target)
        asyncio.run(session._guard_request(cast("Any", route)))
        assert route.continued is True
        assert route.aborted is None

    assert calls == []
    asyncio.run(session.assert_policy_compliant())
    telemetry.close()


def test_standard_mode_latches_rejected_top_level_navigation() -> None:
    telemetry = NetworkTelemetryRecorder(SecretRegistry())
    session = BrowserSession(BrowserOptions(), _deny, _allow, telemetry)
    route = FakeRoute("https://blocked.example.test/private", navigation=True)

    asyncio.run(session._guard_request(cast("Any", route)))

    with pytest.raises(BrowserLifecycleError, match="outbound network policy"):
        asyncio.run(session.close())
