"""Complete semantic snapshot extraction tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from plantain.activities.ui_errors import SnapshotError
from plantain.activities.ui_snapshot_complete import (
    TRANSPORT_PAYLOAD_RESERVE_BYTES,
    TRANSPORT_RECORDS,
    CompleteSemanticSnapshotExtractor,
)
from plantain.activities.ui_telemetry import NetworkTelemetryRecorder
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

FRAME_COUNT_BEYOND_LEGACY_LIMIT = 101
DOM_RECORD_COUNT = 751
METADATA_RECORD_COUNT = 2_000
NETWORK_RECORD_COUNT = 501


class FakeHandle:
    def __init__(self, records: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        self.records = records
        self.metadata = metadata
        self.disposed = False
        self.observer_disposed = False
        self.mutated = False
        self.position = 0
        self.batch_sizes: list[int] = []
        self.batch_bytes: list[int] = []
        self.max_bytes: int | None = None

    async def evaluate(self, expression: str, argument: Any = None) -> Any:
        if expression == "capture => capture.metadata":
            return dict(self.metadata)
        if expression == "capture => capture.dispose()":
            self.observer_disposed = True
            return None
        if "capture.next" in expression:
            assert self.max_bytes is not None
            batch: list[dict[str, Any]] = []
            byte_count = 2
            while self.position < len(self.records) and len(batch) < argument:
                record = self.records[self.position]
                record_bytes = len(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
                )
                separator_bytes = 1 if batch else 0
                if record_bytes + 2 > self.max_bytes:
                    raise RuntimeError("Semantic record exceeds working-set limit")
                if batch and byte_count + separator_bytes + record_bytes > self.max_bytes:
                    break
                batch.append(record)
                byte_count += separator_bytes + record_bytes
                self.position += 1
            self.batch_sizes.append(len(batch))
            self.batch_bytes.append(byte_count)
            return {
                "records": batch,
                "byteCount": byte_count,
                "complete": self.position == len(self.records),
                "mutated": self.mutated,
            }
        raise AssertionError(f"Unexpected handle expression: {expression}")

    async def dispose(self) -> None:
        self.disposed = True


class FakeFrame:
    def __init__(
        self,
        *,
        name: str,
        url: str,
        records: list[dict[str, Any]],
    ) -> None:
        self.name = name
        self.url = url
        self.child_frames: list[FakeFrame] = []
        self.handle = FakeHandle(
            records,
            {
                "baseUrl": url,
                "contentType": "text/html",
                "language": "en",
            },
        )

    async def evaluate_handle(self, expression: str, max_bytes: int) -> FakeHandle:
        assert "(maxBytes) =>" in expression
        self.handle.max_bytes = max_bytes
        return self.handle


class FakePage:
    def __init__(self, main_frame: FakeFrame) -> None:
        self.main_frame = main_frame
        self.url = main_frame.url

    async def title(self) -> str:
        return "Complete Test Page"


def _button_record(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "tag": "button",
        "role": "button",
        "accessibleName": f"Action {index}",
        "text": f"Action {index}",
        "id": f"action-{index}",
        "attributes": {},
        "urlAttributes": {},
        "state": {"visible": True},
        "selectOptions": None,
        "shadowDepth": 0,
        "parentIndex": None if index == 0 else 0,
        "domDepth": 0 if index == 0 else 1,
        "hasOpenShadowRoot": False,
    }


def test_capture_has_no_frame_record_or_network_total_limit(tmp_path: Path) -> None:
    secrets = SecretRegistry(sensitive_keys=("ip_address",))
    records = [_button_record(index) for index in range(DOM_RECORD_COUNT)]
    records[1]["accessibleName"] = "ip_address value"
    main = FakeFrame(
        name="",
        url="https://example.test/app#/inventory",
        records=records,
    )
    main.child_frames = [
        FakeFrame(
            name=f"frame-{index}",
            url=f"https://example.test/frame/{index}",
            records=[],
        )
        for index in range(FRAME_COUNT_BEYOND_LEGACY_LIMIT)
    ]
    page = FakePage(main)
    telemetry = NetworkTelemetryRecorder(secrets, spool_memory_bytes=1)
    for index in range(NETWORK_RECORD_COUNT):
        telemetry.record_websocket_policy(
            f"wss://example.test/events/{index}",
            allowed=True,
        )
    extractor = CompleteSemanticSnapshotExtractor(
        secrets=secrets,
        snapshots_dir=tmp_path / "snapshots",
        telemetry=telemetry,
    )

    staged = asyncio.run(extractor.capture(cast("Any", page), activity="inventory"))
    manifest = staged.manifest

    assert manifest["captureComplete"] is True
    assert len(manifest["frames"]) == FRAME_COUNT_BEYOND_LEGACY_LIMIT + 1
    assert manifest["content"]["dom"]["recordCount"] == DOM_RECORD_COUNT
    assert manifest["content"]["network"]["recordCount"] == NETWORK_RECORD_COUNT
    assert manifest["content"]["network"]["fromSequence"] == 0
    assert manifest["content"]["network"]["throughSequence"] == NETWORK_RECORD_COUNT
    assert manifest["content"]["aria"] == {
        "format": "semantic-accessibility-records-v1",
        "source": "dom",
        "depth": "unlimited",
        "recordCount": DOM_RECORD_COUNT,
    }
    assert "captureBounds" not in manifest
    assert all(frame.handle.disposed for frame in [main, *main.child_frames])
    assert all(frame.handle.observer_disposed for frame in [main, *main.child_frames])
    assert max(main.handle.batch_sizes) <= TRANSPORT_RECORDS
    assert main.handle.max_bytes == 1_048_576 - TRANSPORT_PAYLOAD_RESERVE_BYTES
    assert max(main.handle.batch_bytes) <= main.handle.max_bytes

    chunk_paths = {chunk.relative_file: chunk.path for chunk in staged.chunks}
    dom_descriptors = [
        item for item in manifest["content"]["dom"]["chunks"] if item["frameId"] == "main"
    ]
    dom_records = [
        record
        for item in sorted(dom_descriptors, key=lambda value: value["sequence"])
        for record in json.loads(chunk_paths[item["file"]].read_text())["records"]
    ]
    serialized_records = json.dumps(dom_records)
    assert len(dom_records) == DOM_RECORD_COUNT
    assert dom_records[0]["parentIndex"] is None
    assert dom_records[1]["parentIndex"] == 0
    assert REDACTED in serialized_records
    assert "ip_address value" not in serialized_records

    extractor.commit_telemetry()
    assert telemetry.committed_cursor == NETWORK_RECORD_COUNT
    staged.cleanup()
    telemetry.close()


def test_capture_rejects_dom_mutation_and_cleans_private_stage(tmp_path: Path) -> None:
    secrets = SecretRegistry()
    frame = FakeFrame(
        name="",
        url="https://example.test/app",
        records=[_button_record(1)],
    )
    frame.handle.mutated = True
    telemetry = NetworkTelemetryRecorder(secrets)
    extractor = CompleteSemanticSnapshotExtractor(
        secrets=secrets,
        snapshots_dir=tmp_path / "snapshots",
        telemetry=telemetry,
    )

    with pytest.raises(SnapshotError, match="DOM changed"):
        asyncio.run(extractor.capture(cast("Any", FakePage(frame)), activity="inventory"))

    assert frame.handle.observer_disposed
    assert frame.handle.disposed
    assert not list((tmp_path / "snapshots" / ".staging").iterdir())
    telemetry.close()


def test_capture_rejects_oversized_individual_record_atomically(
    tmp_path: Path,
) -> None:
    secrets = SecretRegistry()
    record = _button_record(0)
    record["text"] = "x" * 70_000
    frame = FakeFrame(
        name="",
        url="https://example.test/app",
        records=[record],
    )
    telemetry = NetworkTelemetryRecorder(secrets)
    extractor = CompleteSemanticSnapshotExtractor(
        secrets=secrets,
        snapshots_dir=tmp_path / "snapshots",
        telemetry=telemetry,
        working_set_bytes=65_536,
    )

    with pytest.raises(SnapshotError, match="complete semantic snapshot"):
        asyncio.run(extractor.capture(cast("Any", FakePage(frame)), activity="inventory"))

    assert frame.handle.observer_disposed
    assert frame.handle.disposed
    assert not list((tmp_path / "snapshots" / ".staging").iterdir())
    telemetry.close()


def test_capture_rejects_cumulative_metadata_growth_atomically(tmp_path: Path) -> None:
    secrets = SecretRegistry()
    frame = FakeFrame(
        name="",
        url="https://example.test/app",
        records=[_button_record(index) for index in range(METADATA_RECORD_COUNT)],
    )
    telemetry = NetworkTelemetryRecorder(secrets)
    extractor = CompleteSemanticSnapshotExtractor(
        secrets=secrets,
        snapshots_dir=tmp_path / "snapshots",
        telemetry=telemetry,
        working_set_bytes=65_536,
    )

    with pytest.raises(SnapshotError, match="PLANTAIN_SNAPSHOT_WORKING_SET_BYTES"):
        asyncio.run(extractor.capture(cast("Any", FakePage(frame)), activity="inventory"))

    assert frame.handle.observer_disposed
    assert frame.handle.disposed
    assert not list((tmp_path / "snapshots" / ".staging").iterdir())
    telemetry.close()
