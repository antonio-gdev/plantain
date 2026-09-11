"""Complete, secret-safe browser telemetry tests."""

from __future__ import annotations

import os
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

from plantain.activities.ui_errors import SnapshotError
from plantain.activities.ui_telemetry import NetworkTelemetryRecorder
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

REQUEST_COUNT = 1_001
EVENTS_PER_REQUEST = 2
READ_BATCH_SIZE = 128


class FakeContext:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


class FakeRequest:
    def __init__(self, *, url: str, failure: str | None = None) -> None:
        self.url = url
        self.method = "POST"
        self.resource_type = "xhr"
        self.headers = {
            "authorization": "Bearer top-secret-value",
            "content-type": "application/json",
        }
        self.redirected_from = None
        self.failure = failure
        self.timing = {"startTime": 1.0, "responseEnd": 2.0}

    def is_navigation_request(self) -> bool:
        return False

    @property
    def post_data(self) -> str:
        raise AssertionError("Telemetry must never access request bodies")


class FakeResponse:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request
        self.url = request.url
        self.status = 200
        self.status_text = "OK"
        self.headers = {"set-cookie": "session=top-secret-value", "x-result": "ok"}
        self.from_service_worker = False

    async def body(self) -> bytes:
        raise AssertionError("Telemetry must never access response bodies")


def test_records_complete_lifecycle_without_bodies_and_redacts_secrets() -> None:
    secrets = SecretRegistry()
    secrets.observe({"password": "top-secret-value"})
    recorder = NetworkTelemetryRecorder(secrets, spool_memory_bytes=1)
    context = FakeContext()
    recorder.attach(cast("Any", context))
    request = FakeRequest(url="https://example.test/items?token=top-secret-value")
    response = FakeResponse(request)

    context.handlers["request"](request)
    context.handlers["response"](response)
    context.handlers["requestfinished"](request)

    events = [event for batch in recorder.batches(batch_size=2) for event in batch]
    assert [event["event"] for event in events] == [
        "request",
        "response",
        "requestFinished",
    ]
    query = parse_qs(urlsplit(events[0]["url"]).query)
    assert query["token"] == [REDACTED]
    assert events[0]["headers"]["authorization"] == REDACTED
    assert events[1]["headers"]["set-cookie"] == REDACTED
    assert all("body" not in event and "postData" not in event for event in events)
    recorder.close()


def test_close_releases_a_rolled_spool_file_descriptor() -> None:
    recorder = NetworkTelemetryRecorder(SecretRegistry(), spool_memory_bytes=1)
    recorder.record_websocket_policy("wss://example.test/socket", allowed=True)
    stream = recorder._stream
    descriptor = stream.fileno()
    os.fstat(descriptor)

    recorder.close()

    assert stream.closed
    with pytest.raises(OSError):
        os.fstat(descriptor)
    recorder.close()


def test_records_failures_and_returns_every_event_across_batches() -> None:
    recorder = NetworkTelemetryRecorder(SecretRegistry(), spool_memory_bytes=1)
    context = FakeContext()
    recorder.attach(cast("Any", context))

    for index in range(REQUEST_COUNT):
        request = FakeRequest(
            url=f"https://example.test/items/{index}",
            failure="connection reset",
        )
        context.handlers["request"](request)
        context.handlers["requestfailed"](request)

    batches = list(recorder.batches(batch_size=READ_BATCH_SIZE))
    events = [event for batch in batches for event in batch]
    expected_events = REQUEST_COUNT * EVENTS_PER_REQUEST
    expected_batches = (expected_events + READ_BATCH_SIZE - 1) // READ_BATCH_SIZE
    assert len(batches) == expected_batches
    assert len(events) == expected_events
    assert recorder.count == expected_events
    assert events[-1]["event"] == "requestFailed"
    recorder.close()


def test_network_batches_apply_byte_backpressure_without_loss() -> None:
    recorder = NetworkTelemetryRecorder(SecretRegistry(), spool_memory_bytes=1)
    for index in range(3):
        recorder.record_websocket_policy(
            f"wss://example.test/{index}/" + ("x" * 600),
            allowed=True,
        )

    batches = list(recorder.batches(batch_size=10, max_batch_bytes=1_000))

    assert [len(batch) for batch in batches] == [1, 1, 1]
    assert [event["sequence"] for batch in batches for event in batch] == [1, 2, 3]
    recorder.close()


def test_commit_compacts_prefix_and_preserves_only_new_deltas() -> None:
    recorder = NetworkTelemetryRecorder(SecretRegistry(), spool_memory_bytes=1)
    recorder.record_websocket_policy("wss://example.test/first", allowed=True)
    first_cursor = recorder.count
    recorder.record_websocket_policy("wss://example.test/second", allowed=True)

    recorder.commit(first_cursor)

    remaining = [event for batch in recorder.batches() for event in batch]
    assert [event["sequence"] for event in remaining] == [2]
    assert remaining[0]["url"] == "wss://example.test/second"
    recorder.commit(recorder.count)
    assert list(recorder.batches()) == []
    recorder.close()


def test_spool_and_unresolved_request_limits_fail_closed() -> None:
    spool_limited = NetworkTelemetryRecorder(
        SecretRegistry(),
        max_spool_bytes=32,
    )
    spool_limited.record_websocket_policy(
        "wss://example.test/a-long-websocket-target",
        allowed=True,
    )
    with pytest.raises(SnapshotError, match="incomplete"):
        list(spool_limited.batches())
    assert spool_limited.count == 0
    spool_limited.close()

    correlation_limited = NetworkTelemetryRecorder(
        SecretRegistry(),
        max_request_correlations=1,
    )
    context = FakeContext()
    correlation_limited.attach(cast("Any", context))
    first = FakeRequest(url="https://example.test/first")
    second = FakeRequest(url="https://example.test/second")
    context.handlers["request"](first)
    context.handlers["request"](second)
    with pytest.raises(SnapshotError, match="incomplete"):
        list(correlation_limited.batches())
    correlation_limited.close()
