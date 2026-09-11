"""Security and resource-boundary tests for the asynchronous API client."""

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
TOO_SMALL_LIMIT = 4


class RecordingPolicy:
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


def test_cross_origin_redirect_revalidates_and_strips_credentials() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://api-two.example/final"},
            )
        return httpx.Response(
            200,
            json={"ok": True},
            headers={
                "set-cookie": "session=hidden",
                "www-authenticate": "Bearer hidden",
            },
        )

    session, policy = _session(handler)
    try:
        response = asyncio.run(
            session.request(
                method=HttpMethod.GET,
                url="https://api-one.example/start",
                headers={"Authorization": "Bearer hidden", "Cookie": "session=hidden"},
                query={"token": "secret-query"},
            )
        )
    finally:
        asyncio.run(session.close())

    assert policy.urls == [
        "https://api-one.example/start?token=secret-query",
        "https://api-two.example/final",
    ]
    assert "authorization" not in requests[1].headers
    assert "cookie" not in requests[1].headers
    assert requests[1].url.query == b""
    assert "secret-query" not in response.request_url
    assert "set-cookie" not in response.headers
    assert "www-authenticate" not in response.headers


def test_https_to_http_redirect_is_blocked() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://api.example/final"})

    session, _policy = _session(handler)
    try:
        with pytest.raises(ApiActivityError, match="HTTPS-to-HTTP"):
            asyncio.run(
                session.request(
                    method=HttpMethod.GET,
                    url="https://api.example/start",
                )
            )
    finally:
        asyncio.run(session.close())


def test_streamed_response_cannot_exceed_explicit_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345")

    session, _policy = _session(handler)
    try:
        with pytest.raises(ApiActivityError, match="response exceeds"):
            asyncio.run(
                session.request(
                    method=HttpMethod.GET,
                    url="https://api.example/data",
                    max_response_bytes=TOO_SMALL_LIMIT,
                )
            )
    finally:
        asyncio.run(session.close())
