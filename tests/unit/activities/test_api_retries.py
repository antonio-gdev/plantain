"""Connection retry safety and URL-policy revalidation tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from plantain.activities.api.client import ApiActivityError, ApiSession
from plantain.models.api import HttpMethod
from plantain.security.url_policy import UrlPolicy

RESPONSE_LIMIT = 1_024
HTTP_OK = 200
EXPECTED_RETRY_REQUESTS = 2


class RecordingPolicy:
    """Record validation calls without resolving external DNS."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def validate(self, url: str) -> str:
        self.urls.append(url)
        return url


def _session(handler: Any) -> tuple[ApiSession, RecordingPolicy]:
    settings = SimpleNamespace(
        api_max_response_bytes=RESPONSE_LIMIT,
        api_timeout_seconds=1.0,
        api_connect_timeout_seconds=1.0,
    )
    policy = RecordingPolicy()
    session = ApiSession(cast("Any", settings), cast("UrlPolicy", policy))
    session._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    return session, policy


def test_get_connection_failure_retries_once_after_policy_revalidation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("simulated connection failure", request=request)
        return httpx.Response(200, json={"ok": True})

    session, policy = _session(handler)
    try:
        response = asyncio.run(
            session.request(method=HttpMethod.GET, url="https://api.example/resource")
        )
    finally:
        asyncio.run(session.close())

    assert response.status_code == HTTP_OK
    assert len(requests) == EXPECTED_RETRY_REQUESTS
    assert policy.urls == [
        "https://api.example/resource",
        "https://api.example/resource",
    ]


def test_post_connection_failure_is_not_retried_or_exposed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("transport detail must stay private", request=request)

    sensitive_url = "https://api.example/private-secret-path"
    session, policy = _session(handler)
    try:
        with pytest.raises(ApiActivityError) as raised:
            asyncio.run(session.request(method=HttpMethod.POST, url=sensitive_url))
    finally:
        asyncio.run(session.close())

    assert len(requests) == 1
    assert policy.urls == [sensitive_url]
    assert "private-secret-path" not in str(raised.value)
    assert "transport detail" not in str(raised.value)
