"""Pooled, bounded HTTP transport for privileged reporting integrations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import httpx

from plantain.errors import ConfigurationError
from plantain.security.url_policy import UrlPolicy


@dataclass(frozen=True, slots=True)
class PostResponse:
    """Bounded response body returned by a reporting POST."""

    status_code: int
    content: bytes


class ReportingTransportError(Exception):
    """Value-free transport failure carrying only a safe lifecycle stage."""

    def __init__(
        self,
        stage: str,
        *,
        delivery_state: Literal["ambiguous", "not_sent"] = "ambiguous",
    ) -> None:
        super().__init__(stage)
        self.stage = stage
        self.delivery_state = delivery_state


class BoundedHttpPoster:
    """Share connections while rejecting redirects, proxies, and oversized responses."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_connections: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_response_bytes <= 0 or max_connections <= 0:
            raise ValueError("Reporting HTTP limits must be positive")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_connections = max_connections
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._request_slots = asyncio.Semaphore(max_connections)

    async def post_json(
        self,
        url: str,
        content: bytes,
        *,
        token: str,
        policy: UrlPolicy,
        operation: str,
    ) -> PostResponse:
        await self._validate(policy, url, operation)
        client = await self._get_client()
        try:
            async with (
                self._request_slots,
                client.stream(
                    "POST",
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    content=content,
                ) as response,
            ):
                return await self._consume(response, operation)
        except httpx.RequestError as exc:
            raise _transport_failure(operation, exc) from exc

    async def post_file(
        self,
        url: str,
        content: bytes,
        *,
        token: str,
        policy: UrlPolicy,
        operation: str,
    ) -> PostResponse:
        await self._validate(policy, url, operation)
        client = await self._get_client()
        try:
            async with (
                self._request_slots,
                client.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    files={"file": ("scenario-result.json", content, "application/json")},
                ) as response,
            ):
                return await self._consume(response, operation)
        except httpx.RequestError as exc:
            raise _transport_failure(operation, exc) from exc

    async def close(self) -> None:
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _consume(self, response: httpx.Response, operation: str) -> PostResponse:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_response_bytes:
            raise ReportingTransportError(f"{operation}_response_limit")
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            if len(buffer) + len(chunk) > self._max_response_bytes:
                raise ReportingTransportError(f"{operation}_response_limit")
            buffer.extend(chunk)
        return PostResponse(status_code=response.status_code, content=bytes(buffer))

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    follow_redirects=False,
                    http2=True,
                    limits=httpx.Limits(
                        max_connections=self._max_connections,
                        max_keepalive_connections=self._max_connections,
                    ),
                    timeout=httpx.Timeout(self._timeout_seconds),
                    transport=self._transport,
                    trust_env=False,
                )
            return self._client

    @staticmethod
    async def _validate(policy: UrlPolicy, url: str, operation: str) -> None:
        try:
            await policy.validate(url)
        except ConfigurationError as exc:
            raise ReportingTransportError(
                f"{operation}_policy",
                delivery_state="not_sent",
            ) from exc


def _transport_failure(
    operation: str,
    error: httpx.RequestError,
) -> ReportingTransportError:
    suffix = "timeout" if isinstance(error, httpx.TimeoutException) else "network"
    not_sent = isinstance(
        error,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
    )
    return ReportingTransportError(
        f"{operation}_{suffix}",
        delivery_state="not_sent" if not_sent else "ambiguous",
    )


__all__ = ["BoundedHttpPoster", "PostResponse", "ReportingTransportError"]
