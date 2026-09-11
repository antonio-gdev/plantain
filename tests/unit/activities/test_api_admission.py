"""Global API request admission tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.api.client import ApiSession, BoundedResponse
from plantain.engine.admission import ResourceAdmission, ResourceKind
from plantain.models.api import HttpMethod
from plantain.security.url_policy import UrlPolicy

HTTP_OK = 200


class AllowPolicy:
    async def validate(self, url: str) -> str:
        return url


def test_sessions_share_global_request_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        api_max_response_bytes=1_024,
        api_timeout_seconds=1.0,
        api_connect_timeout_seconds=1.0,
        max_api_requests=1,
    )
    admission = ResourceAdmission(settings)
    sessions = [
        ApiSession(
            cast("Any", settings),
            cast("UrlPolicy", AllowPolicy()),
            admission=admission,
        )
        for _ in range(2)
    ]
    active = 0
    peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(
        _session: ApiSession,
        _client: Any,
        *,
        method: HttpMethod,
        url: str,
        headers: dict[str, str],
        content: bytes | None,
        timeout_seconds: float,
        response_limit: int,
    ) -> BoundedResponse:
        del headers, content, timeout_seconds, response_limit
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return BoundedResponse(
            request_url=url,
            method=method,
            status_code=HTTP_OK,
            headers={},
            content=b"",
            elapsed_ms=0,
        )

    monkeypatch.setattr(ApiSession, "_send_with_safe_retries", send)

    async def exercise() -> list[BoundedResponse]:
        tasks = [
            asyncio.create_task(
                session.request(
                    method=HttpMethod.GET,
                    url=f"https://api.example.test/{index}",
                )
            )
            for index, session in enumerate(sessions)
        ]
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert peak == admission.limit(ResourceKind.API_REQUEST)
        release.set()
        try:
            return await asyncio.gather(*tasks)
        finally:
            await asyncio.gather(*(session.close() for session in sessions))

    responses = asyncio.run(exercise())

    assert [response.status_code for response in responses] == [HTTP_OK, HTTP_OK]
