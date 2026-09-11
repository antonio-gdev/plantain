"""Complete bounded API transport behavior without external network access."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from plantain.activities.api import client as api_client
from plantain.activities.api.client import (
    ApiActivityError,
    ApiSession,
    BoundedResponse,
    assert_expected_status,
    build_request_url,
)
from plantain.models.api import HttpMethod
from plantain.security.redaction import REDACTED
from plantain.security.url_policy import UrlPolicy

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_FOUND = 302
HTTP_BAD_REQUEST = 400
RESPONSE_LIMIT = 1_024
SMALL_LIMIT = 4
EXPECTED_REDIRECT_REQUESTS = 6
EXPECTED_RETRY_ATTEMPTS = 2
EXPECTED_REDIRECT_CHAIN_REQUESTS = 2
JSON_DEPTH_TEST_LIMIT = 2
JSON_ITEM_TEST_LIMIT = 3
MINIMUM_CHAIN_ELAPSED_MS = 15
SHORT_TIMEOUT_SECONDS = 0.02


class RecordingPolicy:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def validate(self, url: str) -> str:
        self.urls.append(url)
        return url


def _settings(*, response_limit: int = RESPONSE_LIMIT) -> Any:
    return SimpleNamespace(
        api_max_response_bytes=response_limit,
        api_timeout_seconds=1.0,
        api_connect_timeout_seconds=0.5,
    )


def _session(
    handler: Any,
    *,
    response_limit: int = RESPONSE_LIMIT,
) -> tuple[ApiSession, RecordingPolicy]:
    policy = RecordingPolicy()
    session = ApiSession(
        cast("Any", _settings(response_limit=response_limit)),
        cast("UrlPolicy", policy),
    )
    session._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    return session, policy


def _bounded(content: bytes, content_type: str = "") -> BoundedResponse:
    headers = {"content-type": content_type} if content_type else {}
    return BoundedResponse(
        request_url="https://example.test/resource",
        method=HttpMethod.GET,
        status_code=HTTP_OK,
        headers=headers,
        content=content,
        elapsed_ms=1,
    )


def test_api_activity_error_accepts_only_bounded_safe_detail_keys() -> None:
    error = ApiActivityError(
        "safe failure",
        safe_details={
            "failure_stage": "x" * 600,
            "http_status": HTTP_BAD_REQUEST,
        },
    )

    assert error.safe_details == {
        "failure_stage": "x" * 500,
        "http_status": HTTP_BAD_REQUEST,
    }
    with pytest.raises(ValueError, match="Unsupported safe error detail"):
        ApiActivityError("invalid", safe_details={"response_body": "forbidden"})


def test_bounded_response_decodes_empty_json_text_and_binary_content() -> None:
    binary = b"\x00\x01\x02"

    assert _bounded(b"").decoded_body() is None
    assert _bounded(b'{"ok":true}', "application/json").decoded_body() == {"ok": True}
    assert _bounded(b"hello", "text/plain").decoded_body() == "hello"
    assert _bounded(b"<ok/>", "application/xml").decoded_body() == "<ok/>"
    assert _bounded(b"name=Ada", "application/x-www-form-urlencoded").decoded_body() == ("name=Ada")
    assert _bounded(binary).decoded_body() == {
        "contentType": "application/octet-stream",
        "length": len(binary),
        "sha256": hashlib.sha256(binary).hexdigest(),
    }


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"not-json", "application/json"),
        (b"\xff", "application/problem+json"),
        (b'{"value":NaN}', "application/json"),
        (b'{"value":Infinity}', "application/json"),
        (b'{"value":1e9999}', "application/json"),
    ],
)
def test_bounded_response_rejects_invalid_declared_json(
    content: bytes,
    content_type: str,
) -> None:
    with pytest.raises(ApiActivityError, match="invalid JSON"):
        _bounded(content, content_type).decoded_body()


def test_bounded_response_rejects_excessive_json_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_client,
        "MAX_DECODED_JSON_DEPTH",
        JSON_DEPTH_TEST_LIMIT,
    )
    with pytest.raises(ApiActivityError, match="nesting depth"):
        _bounded(
            b'{"one":{"two":{"three":1}}}',
            "application/json",
        ).decoded_body()

    monkeypatch.setattr(
        api_client,
        "MAX_DECODED_JSON_ITEMS",
        JSON_ITEM_TEST_LIMIT,
    )
    with pytest.raises(ApiActivityError, match="decoded item"):
        _bounded(b"[1,2,3]", "application/json").decoded_body()


def test_body_encoding_supports_auto_json_form_text_and_none() -> None:
    headers: dict[str, str] = {}
    assert ApiSession._encode_body(None, "auto", headers) is None

    headers = {}
    assert ApiSession._encode_body({"name": "Ada"}, "auto", headers) == b'{"name":"Ada"}'
    assert headers == {"Content-Type": "application/json"}

    headers = {"content-type": "application/x-www-form-urlencoded"}
    assert (
        ApiSession._encode_body(
            {"status": ["available", "pending"]},
            "auto",
            headers,
        )
        == b"status=available&status=pending"
    )

    headers = {}
    assert ApiSession._encode_body("plain", "auto", headers) == b"plain"
    assert headers == {"Content-Type": "text/plain; charset=utf-8"}


@pytest.mark.parametrize(
    ("body", "body_format", "message"),
    [
        ({"unsupported": {1, 2}}, "json", "not JSON serializable"),
        ({"unsupported": float("nan")}, "json", "not JSON serializable"),
        (["not", "mapping"], "form", "must be mappings"),
        ({"not": "text"}, "text", "must be strings"),
        ("value", "binary", "Unsupported request body format"),
    ],
)
def test_body_encoding_rejects_invalid_shapes(
    body: object,
    body_format: str,
    message: str,
) -> None:
    with pytest.raises(ApiActivityError, match=message):
        ApiSession._encode_body(body, cast("Any", body_format), {})


def test_request_rejects_oversized_body_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not receive oversized request")

    monkeypatch.setattr(api_client, "MAX_REQUEST_BODY_BYTES", SMALL_LIMIT)
    session, policy = _session(unexpected)
    try:
        with pytest.raises(ApiActivityError, match="Request body exceeds"):
            asyncio.run(
                session.request(
                    method=HttpMethod.POST,
                    url="https://api.example.test/items",
                    body="12345",
                    body_format="text",
                )
            )
    finally:
        asyncio.run(session.close())

    assert policy.urls == ["https://api.example.test/items"]


@pytest.mark.parametrize("status", [301, 302, 303])
def test_post_redirect_becomes_get_and_drops_entity_headers(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"location": "/final"})
        return httpx.Response(HTTP_OK, json={"ok": True})

    session, _policy = _session(handler)
    try:
        asyncio.run(
            session.request(
                method=HttpMethod.POST,
                url="https://api.example.test/start",
                headers={"Content-Type": "application/json", "X-Trace": "safe"},
                query={"page": 1},
                body={"name": "Ada"},
            )
        )
    finally:
        asyncio.run(session.close())

    assert requests[1].method == "GET"
    assert requests[1].content == b""
    assert "content-type" not in requests[1].headers
    assert "content-length" not in requests[1].headers
    assert requests[1].headers["x-trace"] == "safe"
    assert requests[1].url.query == b""


def test_temporary_redirect_preserves_method_body_and_same_origin_credentials() -> None:
    requests: list[httpx.Request] = []
    observed_value = "synthetic-credential-value"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(307, headers={"location": "/final"})
        return httpx.Response(HTTP_OK, json={"ok": True})

    session, _policy = _session(handler)
    try:
        asyncio.run(
            session.request(
                method=HttpMethod.POST,
                url="https://api.example.test/start",
                headers={"Authorization": f"Bearer {observed_value}"},
                body={"name": "Ada"},
            )
        )
    finally:
        asyncio.run(session.close())

    assert requests[1].method == "POST"
    assert requests[1].content == b'{"name":"Ada"}'
    assert requests[1].headers["authorization"] == f"Bearer {observed_value}"


def test_redirect_limit_is_enforced() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "/again"})

    session, _policy = _session(handler)
    try:
        with pytest.raises(ApiActivityError, match="redirect limit"):
            asyncio.run(
                session.request(
                    method=HttpMethod.GET,
                    url="https://api.example.test/start",
                )
            )
    finally:
        asyncio.run(session.close())

    assert len(requests) == EXPECTED_REDIRECT_REQUESTS


def test_redirect_chain_shares_one_response_byte_budget() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                HTTP_FOUND,
                headers={"location": "/final"},
                content=b"123",
            )
        return httpx.Response(HTTP_OK, content=b"45")

    session, _policy = _session(handler)
    try:
        with pytest.raises(ApiActivityError, match="response exceeds"):
            asyncio.run(
                session.request(
                    method=HttpMethod.GET,
                    url="https://api.example.test/start",
                    max_response_bytes=SMALL_LIMIT,
                )
            )
    finally:
        asyncio.run(session.close())

    assert len(requests) == EXPECTED_REDIRECT_CHAIN_REQUESTS


def test_redirect_chain_reports_total_elapsed_time() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(0.01)
        if len(requests) == 1:
            return httpx.Response(HTTP_FOUND, headers={"location": "/final"})
        return httpx.Response(HTTP_OK)

    session, _policy = _session(handler)
    try:
        response = asyncio.run(
            session.request(
                method=HttpMethod.GET,
                url="https://api.example.test/start",
            )
        )
    finally:
        asyncio.run(session.close())

    assert response.elapsed_ms >= MINIMUM_CHAIN_ELAPSED_MS


def test_redirect_chain_uses_one_wall_clock_deadline() -> None:
    requests: list[httpx.Request] = []
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(HTTP_FOUND, headers={"location": "/final"})
        await never.wait()
        raise AssertionError("unreachable")

    session, _policy = _session(handler)
    try:
        with pytest.raises(ApiActivityError, match=r"timed out after 0\.02 seconds"):
            asyncio.run(
                session.request(
                    method=HttpMethod.GET,
                    url="https://api.example.test/start",
                    timeout_seconds=SHORT_TIMEOUT_SECONDS,
                )
            )
    finally:
        asyncio.run(session.close())

    assert len(requests) == EXPECTED_REDIRECT_CHAIN_REQUESTS


def test_redirect_status_without_location_is_returned() -> None:
    session, _policy = _session(lambda _request: httpx.Response(HTTP_FOUND))
    try:
        response = asyncio.run(
            session.request(
                method=HttpMethod.GET,
                url="https://api.example.test/start",
            )
        )
    finally:
        asyncio.run(session.close())

    assert response.status_code == HTTP_FOUND


def test_lazy_client_is_reused_and_closed_without_proxy_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class Client:
        def __init__(self, **options: object) -> None:
            captured.append(options)
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(api_client.httpx, "AsyncClient", Client)
    session = ApiSession(cast("Any", _settings()), UrlPolicy(allow_private_networks=True))

    async def exercise() -> tuple[Any, Any]:
        first = await session._get_client()
        second = await session._get_client()
        await session.close()
        await session.close()
        return first, second

    first, second = asyncio.run(exercise())

    assert first is second
    assert first.close_calls == 1
    assert captured[0]["follow_redirects"] is False
    assert captured[0]["http2"] is True
    assert captured[0]["trust_env"] is False
    assert session._client is None


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (httpx.ReadTimeout, "timed out after 1 seconds"),
        (httpx.ProtocolError, "failed before a response"),
    ],
)
def test_stream_errors_are_translated_without_transport_details(
    error_type: type[httpx.RequestError],
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("synthetic transport detail", request=request)

    session, _policy = _session(handler)
    try:
        with pytest.raises(ApiActivityError, match=message) as raised:
            asyncio.run(
                session.request(
                    method=HttpMethod.POST,
                    url="https://api.example.test/items",
                )
            )
    finally:
        asyncio.run(session.close())

    assert "synthetic transport detail" not in str(raised.value)


def test_safe_retry_exhaustion_preserves_only_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    sleep_delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectTimeout("synthetic connect detail", request=request)

    async def no_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(api_client.asyncio, "sleep", no_sleep)
    session, policy = _session(handler)
    try:
        with pytest.raises(ApiActivityError, match="could not connect") as raised:
            asyncio.run(
                session.request(
                    method=HttpMethod.HEAD,
                    url="https://api.example.test/items",
                )
            )
    finally:
        asyncio.run(session.close())

    assert len(requests) == EXPECTED_RETRY_ATTEMPTS
    assert len(policy.urls) == EXPECTED_RETRY_ATTEMPTS
    assert sleep_delays == [ApiSession._RETRY_BASE_DELAY_SECONDS]
    assert "synthetic connect detail" not in str(raised.value)


class ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> Any:
        yield b"123"
        yield b"45"

    async def aclose(self) -> None:
        return None


def test_declared_and_streamed_response_limits_are_enforced() -> None:
    declared_session, _policy = _session(
        lambda _request: httpx.Response(
            HTTP_OK,
            headers={"content-length": "5"},
            stream=ChunkStream(),
        )
    )
    try:
        with pytest.raises(ApiActivityError, match="response exceeds"):
            asyncio.run(
                declared_session.request(
                    method=HttpMethod.GET,
                    url="https://api.example.test/items",
                    max_response_bytes=SMALL_LIMIT,
                )
            )
    finally:
        asyncio.run(declared_session.close())

    streamed_session, _policy = _session(
        lambda _request: httpx.Response(HTTP_OK, stream=ChunkStream())
    )
    try:
        with pytest.raises(ApiActivityError, match="response exceeds"):
            asyncio.run(
                streamed_session.request(
                    method=HttpMethod.GET,
                    url="https://api.example.test/items",
                    max_response_bytes=SMALL_LIMIT,
                )
            )
    finally:
        asyncio.run(streamed_session.close())


def test_url_status_and_header_helpers_cover_every_contract_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert build_request_url("https://example.test/items", None) == ("https://example.test/items")
    assert build_request_url("/items", "https://example.test/api/v1/") == (
        "https://example.test/api/v1/items"
    )
    with pytest.raises(ApiActivityError, match="requires baseUrl"):
        build_request_url("items", None)

    assert_expected_status(HTTP_OK, HTTP_OK)
    assert_expected_status(HTTP_CREATED, [HTTP_OK, HTTP_CREATED])
    assert_expected_status(HTTP_OK, SimpleNamespace(minimum=HTTP_OK, maximum=299))
    with pytest.raises(ApiActivityError, match="Unexpected HTTP status") as raised:
        assert_expected_status(HTTP_BAD_REQUEST, [HTTP_OK, HTTP_CREATED])
    assert raised.value.safe_details == {
        "failure_stage": "status_validation",
        "http_status": HTTP_BAD_REQUEST,
    }

    headers = httpx.Headers(
        {
            "Authorization": "Bearer synthetic-value",
            "Set-Cookie": "session=synthetic-value",
            "X-Trace": "safe",
        }
    )
    assert api_client._safe_response_headers(headers) == {"x-trace": "safe"}
    monkeypatch.setattr(api_client, "redact_artifact", lambda _value: [REDACTED])
    assert api_client._safe_response_headers(httpx.Headers({"X-Trace": "safe"})) == {}

    assert api_client._origin("HTTPS://Example.Test:443/path") == (
        "https",
        "example.test",
        443,
    )
