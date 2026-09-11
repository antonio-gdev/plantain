"""Complete, body-free browser network telemetry with bounded memory use."""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Iterator, Mapping
from typing import Any

from playwright.async_api import BrowserContext, Request, Response

from plantain.activities.snapshot_bundle import (
    DEFAULT_MAX_CAPTURE_BYTES,
    DEFAULT_RECORD_CHUNK_BYTES,
)
from plantain.activities.ui_errors import SnapshotError
from plantain.security.secrets import SecretRegistry

JsonObject = dict[str, Any]
DEFAULT_SPOOL_MEMORY_BYTES = 1_048_576
DEFAULT_MAX_SPOOL_BYTES = DEFAULT_MAX_CAPTURE_BYTES
DEFAULT_MAX_REQUEST_CORRELATIONS = 10_000
DEFAULT_READ_BATCH_SIZE = 250


class NetworkTelemetryRecorder:
    """Record every browser network lifecycle event without storing bodies."""

    def __init__(
        self,
        secrets: SecretRegistry,
        *,
        spool_memory_bytes: int = DEFAULT_SPOOL_MEMORY_BYTES,
        max_spool_bytes: int = DEFAULT_MAX_SPOOL_BYTES,
        max_request_correlations: int = DEFAULT_MAX_REQUEST_CORRELATIONS,
    ) -> None:
        if spool_memory_bytes < 1:
            raise ValueError("spool_memory_bytes must be positive")
        if max_spool_bytes < 1:
            raise ValueError("max_spool_bytes must be positive")
        if max_request_correlations < 1:
            raise ValueError("max_request_correlations must be positive")
        self._secrets = secrets
        self._spool_memory_bytes = spool_memory_bytes
        self._max_spool_bytes = max_spool_bytes
        self._max_request_correlations = max_request_correlations
        self._stream = _open_spool(spool_memory_bytes)
        self._spool_bytes = 0
        self._started_at = time.monotonic()
        self._sequence = 0
        self._committed_sequence = 0
        self._next_request_id = 0
        self._request_ids: dict[int, int] = {}
        self._failure: Exception | None = None
        self._closed = False
        self._attached = False

    @property
    def count(self) -> int:
        return self._sequence

    @property
    def committed_cursor(self) -> int:
        return self._committed_sequence

    def attach(self, context: BrowserContext) -> None:
        """Attach once before any page is created in the browser context."""

        if self._attached:
            raise RuntimeError("Network telemetry is already attached to a browser context")
        if self._closed:
            raise RuntimeError("Closed network telemetry cannot be attached")
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfinished", self._on_request_finished)
        context.on("requestfailed", self._on_request_failed)
        self._attached = True

    def record_websocket_policy(self, url: str, *, allowed: bool) -> None:
        """Record a WebSocket connection decision without message payloads."""

        self._append(
            {
                "event": "websocketAllowed" if allowed else "websocketBlocked",
                "url": self._secrets.redact_url(url),
            }
        )

    def batches(
        self,
        batch_size: int = DEFAULT_READ_BATCH_SIZE,
        *,
        max_batch_bytes: int = DEFAULT_RECORD_CHUNK_BYTES,
        start_sequence: int | None = None,
        end_sequence: int | None = None,
    ) -> Iterator[list[JsonObject]]:
        """Yield one complete, immutable event range in fixed transport batches."""

        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_batch_bytes < 1:
            raise ValueError("max_batch_bytes must be positive")
        self._raise_if_unavailable()
        start, end = _validated_cursor_range(
            self._committed_sequence,
            self._sequence,
            start_sequence,
            end_sequence,
        )
        self._stream.flush()
        end_position = self._stream.tell()
        self._stream.seek(0)
        expected_sequence = start + 1
        batch: list[JsonObject] = []
        batch_bytes = 0
        try:
            while self._stream.tell() < end_position:
                raw = self._stream.readline()
                if not raw:
                    break
                value, sequence = _decoded_event(raw)
                if sequence <= start:
                    continue
                if sequence > end:
                    break
                event_bytes = _validated_event_bytes(
                    raw,
                    sequence,
                    expected_sequence,
                    max_batch_bytes,
                )
                if batch and batch_bytes + event_bytes > max_batch_bytes:
                    yield batch
                    batch = []
                    batch_bytes = 0
                batch.append(value)
                batch_bytes += event_bytes
                expected_sequence += 1
                if len(batch) == batch_size:
                    yield batch
                    batch = []
                    batch_bytes = 0
            if batch:
                yield batch
            if expected_sequence != end + 1:
                raise SnapshotError("Network telemetry range is incomplete")
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError("Network telemetry cannot be read completely") from exc
        finally:
            self._stream.seek(0, 2)

    def commit(self, cursor: int) -> None:
        """Commit a canonically registered cursor and compact its private spool prefix."""

        self._raise_if_unavailable()
        if (
            not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < self._committed_sequence
            or cursor > self._sequence
        ):
            raise ValueError("Network telemetry commit cursor is invalid")
        if cursor == self._committed_sequence:
            return
        self._committed_sequence = cursor
        try:
            self._compact_committed()
        except Exception as exc:  # noqa: BLE001 - future capture must fail closed.
            self._failure = exc

    def close(self) -> None:
        if self._closed:
            return
        self._stream.close()
        self._closed = True

    def _on_request(self, request: Request) -> None:
        request_id = self._request_id(request)
        redirected_from = request.redirected_from
        self._append(
            {
                "event": "request",
                "requestId": request_id,
                "method": request.method,
                "url": self._secrets.redact_url(request.url),
                "resourceType": request.resource_type,
                "navigation": request.is_navigation_request(),
                "headers": request.headers,
                "redirectedFromUrl": (
                    self._secrets.redact_url(redirected_from.url)
                    if redirected_from is not None
                    else None
                ),
            }
        )

    def _on_response(self, response: Response) -> None:
        self._append(
            {
                "event": "response",
                "requestId": self._request_id(response.request),
                "url": self._secrets.redact_url(response.url),
                "status": response.status,
                "statusText": response.status_text,
                "headers": response.headers,
                "fromServiceWorker": response.from_service_worker,
            }
        )

    def _on_request_finished(self, request: Request) -> None:
        request_id = self._request_id(request)
        self._append(
            {
                "event": "requestFinished",
                "requestId": request_id,
                "timing": request.timing,
            }
        )
        self._request_ids.pop(id(request), None)

    def _on_request_failed(self, request: Request) -> None:
        request_id = self._request_id(request)
        self._append(
            {
                "event": "requestFailed",
                "requestId": request_id,
                "failure": self._secrets.redact_text(request.failure or "unknown failure"),
                "timing": request.timing,
            }
        )
        self._request_ids.pop(id(request), None)

    def _request_id(self, request: Request) -> int:
        marker = id(request)
        existing = self._request_ids.get(marker)
        if existing is not None:
            return existing
        if len(self._request_ids) >= self._max_request_correlations:
            self._failure = RuntimeError("Network request correlation capacity was exceeded")
            return 0
        self._next_request_id += 1
        self._request_ids[marker] = self._next_request_id
        return self._next_request_id

    def _append(self, event: Mapping[str, Any]) -> None:
        if self._failure is not None or self._closed:
            return
        try:
            sequence = self._sequence + 1
            value = {
                "sequence": sequence,
                "elapsedMs": round((time.monotonic() - self._started_at) * 1_000, 3),
                **event,
            }
            sanitized = self._secrets.redact(value)
            if not isinstance(sanitized, dict):
                self._failure = TypeError("Sanitized telemetry event must be a mapping")
                return
            payload = (
                json.dumps(
                    sanitized,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            payload_bytes = len(payload.encode("utf-8"))
            if self._spool_bytes + payload_bytes > self._max_spool_bytes:
                self._failure = RuntimeError(
                    "Network telemetry exceeded the snapshot storage envelope"
                )
                return
            self._stream.write(payload)
            self._sequence = sequence
            self._spool_bytes += payload_bytes
        except Exception as exc:  # noqa: BLE001 - capture must retain a fail-closed marker.
            self._failure = exc

    def _raise_if_unavailable(self) -> None:
        if self._closed:
            raise SnapshotError("Network telemetry was closed before snapshot capture")
        if self._failure is not None:
            raise SnapshotError("Network telemetry capture is incomplete") from self._failure

    def _compact_committed(self) -> None:
        replacement = _open_spool(self._spool_memory_bytes)
        retained_bytes = 0
        self._stream.flush()
        self._stream.seek(0)
        try:
            for raw in self._stream:
                _value, sequence = _decoded_event(raw)
                if sequence > self._committed_sequence:
                    replacement.write(raw)
                    retained_bytes += len(raw.encode("utf-8"))
            replacement.flush()
        except (OSError, json.JSONDecodeError, SnapshotError):
            replacement.close()
            self._stream.seek(0, 2)
            raise SnapshotError("Network telemetry cannot be compacted safely") from None
        previous = self._stream
        self._stream = replacement
        self._spool_bytes = retained_bytes
        previous.close()


def _open_spool(max_memory_bytes: int) -> tempfile.SpooledTemporaryFile[str]:
    return tempfile.SpooledTemporaryFile(
        max_size=max_memory_bytes,
        mode="w+t",
        encoding="utf-8",
        newline="\n",
    )


def _validated_cursor_range(
    committed: int,
    current: int,
    start: int | None,
    end: int | None,
) -> tuple[int, int]:
    lower = committed if start is None else start
    upper = current if end is None else end
    if (
        not isinstance(lower, int)
        or isinstance(lower, bool)
        or not isinstance(upper, int)
        or isinstance(upper, bool)
        or lower < committed
        or upper < lower
        or upper > current
    ):
        raise ValueError("Network telemetry cursor range is invalid")
    return lower, upper


def _decoded_event(raw: str) -> tuple[JsonObject, int]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise SnapshotError("Network telemetry contains an invalid event")
    sequence = value.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise SnapshotError("Network telemetry contains an invalid sequence")
    return value, sequence


def _validated_event_bytes(
    raw: str,
    sequence: int,
    expected_sequence: int,
    max_batch_bytes: int,
) -> int:
    if sequence != expected_sequence:
        raise SnapshotError("Network telemetry range is incomplete")
    event_bytes = len(raw.encode("utf-8"))
    if event_bytes > max_batch_bytes:
        raise SnapshotError("Network telemetry event exceeds PLANTAIN_SNAPSHOT_WORKING_SET_BYTES")
    return event_bytes


__all__ = ["NetworkTelemetryRecorder"]
