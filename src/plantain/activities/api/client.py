"""Bounded asynchronous HTTP session with redirect and SSRF enforcement."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Literal, NoReturn
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from plantain.config import Settings
from plantain.engine.admission import ResourceAdmission, ResourceKind
from plantain.errors import ApiError
from plantain.models.api import (
    MAX_HEADER_NAME_LENGTH,
    MAX_HEADER_VALUE_BYTES,
    MAX_REQUEST_HEADERS,
    HttpMethod,
)
from plantain.security.redaction import redact_artifact, redact_url
from plantain.security.url_policy import UrlPolicy

MAX_REQUEST_BODY_BYTES = 67_108_864
MAX_DECODED_JSON_DEPTH = 100
MAX_DECODED_JSON_ITEMS = 100_000
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RUNTIME_MANAGED_HEADERS = frozenset({"content-length", "host", "transfer-encoding"})
_CROSS_ORIGIN_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "accept-charset",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "content-type",
        "range",
        "user-agent",
    }
)


class ApiActivityError(ApiError):
    """Raised for safe HTTP execution and response-boundary failures."""

    _SAFE_DETAIL_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"failure_stage", "http_status", "schema_path", "schema_rule"}
    )

    def __init__(
        self,
        message: str,
        *,
        safe_details: Mapping[str, str | int] | None = None,
    ) -> None:
        super().__init__(message)
        details = dict(safe_details or {})
        unknown = sorted(set(details) - self._SAFE_DETAIL_KEYS)
        if unknown:
            raise ValueError(f"Unsupported safe error detail: {unknown[0]}")
        self.safe_details = {
            key: value[:500] if isinstance(value, str) else value for key, value in details.items()
        }


@dataclass(frozen=True, slots=True)
class BoundedResponse:
    """Fully consumed response proven to be within configured limits."""

    request_url: str
    method: HttpMethod
    status_code: int
    headers: dict[str, str]
    content: bytes
    elapsed_ms: int

    def decoded_body(self) -> Any:
        content_type = self.headers.get("content-type", "").casefold()
        if not self.content:
            return None
        if "json" in content_type:
            return _decode_json_body(self.content)
        if content_type.startswith("text/") or any(
            marker in content_type for marker in ("xml", "yaml", "x-www-form-urlencoded")
        ):
            return self.content.decode("utf-8", errors="replace")
        return {
            "contentType": self.headers.get("content-type", "application/octet-stream"),
            "length": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }


def request_content_type(
    body: Any,
    body_format: Literal["auto", "json", "form", "text"],
    headers: Mapping[str, str],
) -> str | None:
    """Return the media type the request encoder will place on the wire."""

    explicit = next(
        (value for key, value in headers.items() if key.casefold() == "content-type"),
        None,
    )
    if explicit is not None:
        return explicit
    if body is None:
        return None
    selected = body_format
    if selected == "auto":
        selected = "json" if isinstance(body, (dict, list)) else "text"
    return {
        "json": "application/json",
        "form": "application/x-www-form-urlencoded",
        "text": "text/plain; charset=utf-8",
    }.get(selected)


class ApiSession:
    """One cookie-isolated, connection-pooled HTTP session per scenario."""

    _REDIRECT_STATUSES: ClassVar[frozenset[int]] = frozenset({301, 302, 303, 307, 308})
    _SAFE_RETRY_METHODS: ClassVar[frozenset[HttpMethod]] = frozenset(
        {HttpMethod.GET, HttpMethod.HEAD, HttpMethod.OPTIONS}
    )
    _MAX_REDIRECTS: ClassVar[int] = 5
    _SEE_OTHER_STATUS: ClassVar[int] = 303
    _SAFE_RETRY_ATTEMPTS: ClassVar[int] = 2
    _SINGLE_ATTEMPT: ClassVar[int] = 1
    _RETRY_BASE_DELAY_SECONDS: ClassVar[float] = 0.1
    _MAX_CONNECTIONS_PER_SCENARIO: ClassVar[int] = 1

    def __init__(
        self,
        settings: Settings,
        url_policy: UrlPolicy,
        *,
        admission: ResourceAdmission | None = None,
    ) -> None:
        self._settings = settings
        self._url_policy = url_policy
        self._admission = admission or ResourceAdmission(settings)
        self._client: httpx.AsyncClient | None = None

    async def request(
        self,
        *,
        method: HttpMethod,
        url: str,
        headers: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        body_format: Literal["auto", "json", "form", "text"] = "auto",
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
    ) -> BoundedResponse:
        timeout = timeout_seconds or self._settings.api_timeout_seconds
        response_limit = max_response_bytes or self._settings.api_max_response_bytes
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                current_url = _url_with_query(url, query)
                current_url = await self._url_policy.validate(current_url)
                client = await self._get_client()
                request_headers = _normalized_request_headers(headers or {})
                content = self._encode_body(body, body_format, request_headers)
                request_headers = _normalized_request_headers(request_headers)
                if len(content or b"") > MAX_REQUEST_BODY_BYTES:
                    raise ApiActivityError("Request body exceeds the configured byte limit")
                response = await self._request_redirect_chain(
                    client,
                    method=method,
                    url=current_url,
                    headers=request_headers,
                    content=content,
                    timeout_seconds=timeout,
                    response_limit=response_limit,
                )
        except TimeoutError:
            raise ApiActivityError(f"HTTP request timed out after {timeout:g} seconds") from None
        return replace(
            response,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

    async def _request_redirect_chain(
        self,
        client: httpx.AsyncClient,
        *,
        method: HttpMethod,
        url: str,
        headers: dict[str, str],
        content: bytes | None,
        timeout_seconds: float,
        response_limit: int,
    ) -> BoundedResponse:
        current_method = method
        current_url = url
        current_headers = headers
        current_content = content
        remaining_response_bytes = response_limit
        redirects = 0

        while True:
            async with self._admission.acquire(ResourceKind.API_REQUEST):
                response = await self._send_with_safe_retries(
                    client,
                    method=current_method,
                    url=current_url,
                    headers=current_headers,
                    content=current_content,
                    timeout_seconds=timeout_seconds,
                    response_limit=remaining_response_bytes,
                )
            remaining_response_bytes -= len(response.content)
            location = response.headers.get("location")
            if response.status_code not in self._REDIRECT_STATUSES or not location:
                return response
            redirects += 1
            if redirects > self._MAX_REDIRECTS:
                raise ApiActivityError("HTTP redirect limit of 5 was exceeded")
            next_url = await self._url_policy.validate(urljoin(current_url, location))
            if urlsplit(current_url).scheme == "https" and urlsplit(next_url).scheme == "http":
                raise ApiActivityError("HTTPS-to-HTTP redirects are blocked")
            if _origin(current_url) != _origin(next_url):
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key in _CROSS_ORIGIN_HEADER_ALLOWLIST
                }
            if response.status_code == self._SEE_OTHER_STATUS or (
                response.status_code in {301, 302} and current_method is HttpMethod.POST
            ):
                current_method = HttpMethod.GET
                current_content = None
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key.casefold() not in {"content-type", "content-length"}
                }
            current_url = next_url

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            limits = httpx.Limits(
                max_connections=self._MAX_CONNECTIONS_PER_SCENARIO,
                max_keepalive_connections=self._MAX_CONNECTIONS_PER_SCENARIO,
            )
            timeout = httpx.Timeout(
                timeout=self._settings.api_timeout_seconds,
                connect=self._settings.api_connect_timeout_seconds,
            )
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                http2=True,
                limits=limits,
                timeout=timeout,
                trust_env=False,
            )
        return self._client

    async def _send_with_safe_retries(
        self,
        client: httpx.AsyncClient,
        *,
        method: HttpMethod,
        url: str,
        headers: dict[str, str],
        content: bytes | None,
        timeout_seconds: float,
        response_limit: int,
    ) -> BoundedResponse:
        attempts = (
            self._SAFE_RETRY_ATTEMPTS
            if method in self._SAFE_RETRY_METHODS
            else self._SINGLE_ATTEMPT
        )
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._stream_once(
                    client,
                    method=method,
                    url=url,
                    headers=headers,
                    content=content,
                    timeout_seconds=timeout_seconds,
                    response_limit=response_limit,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await self._url_policy.validate(url)
                    await asyncio.sleep(self._RETRY_BASE_DELAY_SECONDS * (attempt + 1))
        raise ApiActivityError(
            "HTTP request could not connect to an approved target"
        ) from last_error

    @staticmethod
    async def _stream_once(
        client: httpx.AsyncClient,
        *,
        method: HttpMethod,
        url: str,
        headers: dict[str, str],
        content: bytes | None,
        timeout_seconds: float,
        response_limit: int,
    ) -> BoundedResponse:
        started = time.monotonic()
        try:
            async with client.stream(
                method.value,
                url,
                headers=headers,
                content=content,
                timeout=timeout_seconds,
            ) as response:
                declared = response.headers.get("content-length")
                _assert_declared_size(declared, response_limit)
                content_buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    _extend_bounded(content_buffer, chunk, response_limit)
                content_bytes = bytes(content_buffer)
                elapsed_ms = round((time.monotonic() - started) * 1000)
                return BoundedResponse(
                    request_url=redact_url(str(response.request.url)),
                    method=method,
                    status_code=response.status_code,
                    headers=_safe_response_headers(response.headers),
                    content=content_bytes,
                    elapsed_ms=elapsed_ms,
                )
        except ApiActivityError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise
        except httpx.TimeoutException as exc:
            raise ApiActivityError(
                f"HTTP request timed out after {timeout_seconds:g} seconds"
            ) from exc
        except httpx.RequestError as exc:
            raise ApiActivityError("HTTP request failed before a response was received") from exc

    @staticmethod
    def _encode_body(
        body: Any,
        body_format: Literal["auto", "json", "form", "text"],
        headers: dict[str, str],
    ) -> bytes | None:
        if body is None:
            return None
        content_type = next(
            (value for key, value in headers.items() if key.casefold() == "content-type"),
            "",
        ).casefold()
        selected = body_format
        if selected == "auto":
            if "x-www-form-urlencoded" in content_type:
                selected = "form"
            elif isinstance(body, (dict, list)) or "json" in content_type:
                selected = "json"
            else:
                selected = "text"
        if selected == "json":
            _set_default_header(headers, "Content-Type", "application/json")
            return _encode_json_body(body)
        if selected == "form":
            if not isinstance(body, dict):
                raise ApiActivityError("Form request bodies must be mappings")
            _set_default_header(
                headers,
                "Content-Type",
                "application/x-www-form-urlencoded",
            )
            return urlencode(body, doseq=True).encode("utf-8")
        if selected == "text":
            if not isinstance(body, str):
                raise ApiActivityError("Text request bodies must be strings")
            _set_default_header(headers, "Content-Type", "text/plain; charset=utf-8")
            return body.encode("utf-8")
        raise ApiActivityError(f"Unsupported request body format: {selected}")


def _set_default_header(headers: dict[str, str], name: str, value: str) -> None:
    if not any(existing.casefold() == name.casefold() for existing in headers):
        headers[name] = value


def _encode_json_body(body: Any) -> bytes:
    encoded = bytearray()
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    try:
        for fragment in encoder.iterencode(body):
            _extend_request_body(encoded, fragment.encode("utf-8"))
    except ApiActivityError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ApiActivityError("Request body is not JSON serializable") from None
    return bytes(encoded)


def _extend_request_body(buffer: bytearray, fragment: bytes) -> None:
    if len(buffer) + len(fragment) > MAX_REQUEST_BODY_BYTES:
        raise ApiActivityError("Request body exceeds the configured byte limit")
    buffer.extend(fragment)


def _decode_json_body(content: bytes) -> Any:
    try:
        value = json.loads(
            content,
            parse_constant=_reject_nonfinite_json,
            parse_float=_finite_json_float,
        )
    except (UnicodeError, ValueError, OverflowError, RecursionError):
        raise ApiActivityError("Response declares JSON but contains invalid JSON") from None
    _validate_decoded_json(value)
    return value


def _reject_nonfinite_json(_value: str) -> NoReturn:
    raise ValueError("Non-finite JSON numbers are unsupported")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON numbers are unsupported")
    return parsed


def _validate_decoded_json(value: Any) -> None:
    pending = [(value, 0)]
    item_count = 0
    while pending:
        current, depth = pending.pop()
        item_count += 1
        if item_count > MAX_DECODED_JSON_ITEMS:
            raise ApiActivityError("Response JSON exceeds its decoded item limit")
        if isinstance(current, dict):
            if current and depth >= MAX_DECODED_JSON_DEPTH:
                raise ApiActivityError("Response JSON exceeds its nesting depth limit")
            pending.extend((nested, depth + 1) for nested in current.values())
        elif isinstance(current, list):
            if current and depth >= MAX_DECODED_JSON_DEPTH:
                raise ApiActivityError("Response JSON exceeds its nesting depth limit")
            pending.extend((nested, depth + 1) for nested in current)


def _normalized_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    if len(headers) > MAX_REQUEST_HEADERS:
        raise ApiActivityError("HTTP request contains too many headers")
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        folded = name.casefold()
        if folded in normalized:
            raise ApiActivityError("HTTP header names must be unique ignoring case")
        if (
            len(name) > MAX_HEADER_NAME_LENGTH
            or _HEADER_NAME.fullmatch(name) is None
            or folded in _RUNTIME_MANAGED_HEADERS
        ):
            raise ApiActivityError("HTTP request contains an invalid or managed header")
        if "\r" in value or "\n" in value:
            raise ApiActivityError("HTTP header value contains a prohibited line break")
        try:
            value_bytes = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ApiActivityError("HTTP header value must be valid UTF-8 text") from None
        if value_bytes > MAX_HEADER_VALUE_BYTES:
            raise ApiActivityError("HTTP header value exceeds its byte limit")
        normalized[folded] = value
    return normalized


def _url_with_query(url: str, query: dict[str, Any] | None) -> str:
    if not query:
        return url
    try:
        return str(httpx.URL(url).copy_merge_params(query))
    except (httpx.InvalidURL, TypeError, ValueError):
        raise ApiActivityError("HTTP query parameters could not be encoded safely") from None


def _assert_declared_size(declared: str | None, response_limit: int) -> None:
    if declared and declared.isdigit() and int(declared) > response_limit:
        raise ApiActivityError("HTTP response exceeds the configured byte limit")


def _extend_bounded(buffer: bytearray, chunk: bytes, response_limit: int) -> None:
    if len(buffer) + len(chunk) > response_limit:
        raise ApiActivityError("HTTP response exceeds the configured byte limit")
    buffer.extend(chunk)


def build_request_url(endpoint: str, base_url: str | None) -> str:
    """Build an absolute URL while preserving an API base path."""

    if urlsplit(endpoint).scheme:
        return endpoint
    if base_url is None:
        raise ApiActivityError("A relative endpoint requires baseUrl")
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def assert_expected_status(actual: int, expected: object) -> None:
    if isinstance(expected, int):
        matches = actual == expected
    elif isinstance(expected, list):
        matches = actual in expected
    else:
        minimum = getattr(expected, "minimum", -1)
        maximum = getattr(expected, "maximum", -1)
        matches = minimum <= actual <= maximum
    if not matches:
        raise ApiActivityError(
            f"Unexpected HTTP status {actual}; request did not meet expectation",
            safe_details={
                "failure_stage": "status_validation",
                "http_status": actual,
            },
        )


def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
    excluded = {"authorization", "proxy-authenticate", "set-cookie", "www-authenticate"}
    cleaned = {key: value for key, value in headers.items() if key.casefold() not in excluded}
    sanitized = redact_artifact(cleaned)
    return sanitized if isinstance(sanitized, dict) else {}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.casefold(), (parts.hostname or "").casefold(), parts.port
