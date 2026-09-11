"""Safe delivery-state classification for privileged reporting POSTs."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest

from plantain.reporting.http import BoundedHttpPoster, ReportingTransportError
from plantain.security.url_policy import UrlPolicy

SYNTHETIC_TOKEN = "synthetic-token"  # noqa: S105 - isolated transport fixture.


class _AllowPolicy:
    async def validate(self, url: str) -> str:
        return url


@pytest.mark.parametrize(
    ("error_type", "expected_stage", "expected_delivery"),
    [
        (httpx.ConnectError, "publish_network", "not_sent"),
        (httpx.ConnectTimeout, "publish_timeout", "not_sent"),
        (httpx.PoolTimeout, "publish_timeout", "not_sent"),
        (httpx.WriteError, "publish_network", "ambiguous"),
        (httpx.ReadError, "publish_network", "ambiguous"),
        (httpx.WriteTimeout, "publish_timeout", "ambiguous"),
        (httpx.ReadTimeout, "publish_timeout", "ambiguous"),
    ],
)
def test_reporting_transport_classifies_delivery_without_exposing_details(
    error_type: type[httpx.RequestError],
    expected_stage: str,
    expected_delivery: str,
) -> None:
    def handler(request: httpx.Request) -> Any:
        raise error_type("sensitive transport detail", request=request)

    poster = BoundedHttpPoster(
        timeout_seconds=1,
        max_response_bytes=1_024,
        max_connections=1,
        transport=httpx.MockTransport(handler),
    )

    async def invoke() -> ReportingTransportError:
        try:
            with pytest.raises(ReportingTransportError) as captured:
                await poster.post_json(
                    "https://jira.example.test/result",
                    b"{}",
                    token=SYNTHETIC_TOKEN,
                    policy=cast("UrlPolicy", _AllowPolicy()),
                    operation="publish",
                )
            return captured.value
        finally:
            await poster.close()

    error = asyncio.run(invoke())
    assert error.stage == expected_stage
    assert error.delivery_state == expected_delivery
    assert "sensitive transport detail" not in str(error)
