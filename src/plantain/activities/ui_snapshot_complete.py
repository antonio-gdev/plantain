"""Complete MCP-like semantic capture for arbitrary web applications."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Frame, Page

from plantain.activities.snapshot_bundle import (
    DEFAULT_MAX_CAPTURE_BYTES,
    DEFAULT_RECORD_CHUNK_BYTES,
    SnapshotBundleBuilder,
    StagedSnapshot,
)
from plantain.activities.ui_errors import SnapshotConsistencyError, SnapshotError
from plantain.activities.ui_telemetry import NetworkTelemetryRecorder
from plantain.security.secrets import SecretRegistry

JsonObject = dict[str, Any]
TRANSPORT_RECORDS = 250
TRANSPORT_PAYLOAD_RESERVE_BYTES = 4_096
MIN_JSON_ARRAY_BYTES = 2
_DYNAMIC_ID = re.compile(r"^(?:-?\d|jqg\d+_)")
_PORTAL_BUILD = re.compile(r"/(?:portalv)[^/]+/", re.IGNORECASE)

_DOM_RECORDS_SCRIPT = r"""
(maxBytes) => {
  if (!Number.isInteger(maxBytes) || maxBytes < 1024) {
    throw new Error("Invalid semantic capture byte limit");
  }
  const encoder = new TextEncoder();
  const fieldCharacterLimit = Math.max(1, Math.floor(maxBytes / 16));
  let remainingFieldCharacters = fieldCharacterLimit;
  const reserve = (amount) => {
    remainingFieldCharacters -= amount;
    if (remainingFieldCharacters < 0) {
      throw new Error("Semantic record exceeds the configured working-set limit");
    }
  };
  const bounded = (value) => {
    const text = String(value);
    reserve(text.length);
    return text;
  };
  const normalize = (value) => {
    if (value === null || value === undefined) return null;
    const clean = bounded(value).replace(/\s+/g, " ").trim();
    return clean || null;
  };
  const implicitRole = (element) => {
    const tag = element.tagName.toLowerCase();
    const type = (normalize(element.getAttribute("type")) || "").toLowerCase();
    if (tag === "button") return "button";
    if (tag === "a" && element.hasAttribute("href")) return "link";
    if (tag === "select") return element.multiple ? "listbox" : "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      if (["button", "image", "reset", "submit"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      return "textbox";
    }
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "img") return "img";
    if (tag === "table") return "table";
    if (tag === "tr") return "row";
    if (tag === "th") return "columnheader";
    if (tag === "td") return "cell";
    if (tag === "nav") return "navigation";
    if (tag === "main") return "main";
    if (tag === "form") return "form";
    if (tag === "dialog") return "dialog";
    return null;
  };
  const appendText = (current, value) => {
    const addition = String(value || "");
    if (!addition) return current;
    const separator = current ? " " : "";
    if (current.length + separator.length + addition.length > fieldCharacterLimit) {
      throw new Error("Semantic text exceeds the configured working-set limit");
    }
    return current + separator + addition;
  };
  const directText = (element) => {
    let text = "";
    for (const node of element.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        text = appendText(text, node.textContent);
      }
    }
    return normalize(text);
  };
  const descendantText = (element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let text = "";
    while (walker.nextNode()) {
      const parent = walker.currentNode.parentElement;
      if (!parent || parent.closest("script,style,template,[hidden]")) continue;
      text = appendText(text, walker.currentNode.textContent);
    }
    return normalize(text);
  };
  const semanticText = (element) => {
    const tag = element.tagName.toLowerCase();
    const role = element.getAttribute("role") || implicitRole(element);
    const textBearing = new Set([
      "a", "button", "caption", "label", "legend", "li", "option", "summary",
      "td", "th", "title"
    ]);
    if (textBearing.has(tag) || role === "heading") return descendantText(element);
    return directText(element);
  };
  const labelledByText = (element) => {
    const raw = normalize(element.getAttribute("aria-labelledby"));
    if (!raw) return null;
    const root = element.getRootNode();
    let text = "";
    for (const id of raw.split(/\s+/)) {
      const target = typeof root.getElementById === "function" ? root.getElementById(id) : null;
      if (target) text = appendText(text, descendantText(target));
    }
    return normalize(text);
  };
  const labelText = (element) => {
    if (element.labels && element.labels.length) {
      let text = "";
      for (const label of element.labels) {
        text = appendText(text, descendantText(label));
      }
      return normalize(text);
    }
    const parent = element.closest("label");
    return parent ? descendantText(parent) : null;
  };
  const accessibleName = (element, fallbackText) => normalize(
    element.getAttribute("aria-label") ||
    labelledByText(element) ||
    labelText(element) ||
    element.getAttribute("alt") ||
    element.getAttribute("title") ||
    element.getAttribute("placeholder") ||
    fallbackText
  );
  const visibility = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return !element.hidden && style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const attributes = (element) => {
    const values = {};
    const urls = {};
    for (const attribute of element.attributes) {
      reserve(32);
      const name = bounded(attribute.name.toLowerCase());
      if (["nonce", "srcdoc", "value"].includes(name) || name.startsWith("on")) continue;
      const rawValue = bounded(attribute.value);
      if (["action", "formaction", "href", "poster", "src"].includes(name)) {
        try {
          const resolved = new URL(rawValue, document.baseURI).href;
          urls[name] = resolved === rawValue ? rawValue : bounded(resolved);
        } catch (_error) {
          urls[name] = rawValue;
        }
      } else {
        values[name] = rawValue;
      }
    }
    return {values, urls};
  };
  const state = (element) => ({
    checked: "checked" in element ? Boolean(element.checked) : null,
    disabled: "disabled" in element ? Boolean(element.disabled) : null,
    expanded: normalize(element.getAttribute("aria-expanded")),
    pressed: normalize(element.getAttribute("aria-pressed")),
    readOnly: "readOnly" in element ? Boolean(element.readOnly) : null,
    required: "required" in element ? Boolean(element.required) : null,
    selected: "selected" in element ? Boolean(element.selected) : null,
    visible: visibility(element)
  });
  const options = (element) => {
    if (element.tagName.toLowerCase() !== "select") return null;
    const result = [];
    let index = 0;
    for (const option of element.options) {
      reserve(64);
      result.push({
        index,
        label: normalize(option.label || option.textContent),
        disabled: Boolean(option.disabled),
        selected: Boolean(option.selected)
      });
      index += 1;
    }
    return result;
  };

  const root = document.documentElement;
  let rootPending = root ? {
    element: root,
    shadowDepth: 0,
    parentIndex: null,
    domDepth: 0
  } : null;
  const cursors = [];
  let nextIndex = 0;
  let mutated = false;
  const observer = root ? new MutationObserver(() => {
    mutated = true;
  }) : null;
  if (observer) {
    observer.observe(root, {
      attributes: true,
      characterData: true,
      childList: true,
      subtree: true
    });
  }
  const enqueueChildren = (element, shadowDepth, parentIndex, domDepth) => {
    const light = element.firstElementChild;
    if (light) cursors.push({next: light, shadowDepth, parentIndex, domDepth});
    const shadow = element.shadowRoot ? element.shadowRoot.firstElementChild : null;
    if (shadow) {
      cursors.push({
        next: shadow,
        shadowDepth: shadowDepth + 1,
        parentIndex,
        domDepth
      });
    }
  };
  const nextElement = () => {
    if (rootPending) {
      const current = rootPending;
      rootPending = null;
      return current;
    }
    while (cursors.length) {
      const cursor = cursors[cursors.length - 1];
      if (!cursor.next) {
        cursors.pop();
        continue;
      }
      const element = cursor.next;
      cursor.next = element.nextElementSibling;
      return {
        element,
        shadowDepth: cursor.shadowDepth,
        parentIndex: cursor.parentIndex,
        domDepth: cursor.domDepth
      };
    }
    return null;
  };
  let pendingRecord = null;
  const serializedRecordBytes = (record) =>
    encoder.encode(JSON.stringify(record)).byteLength;
  const buildRecord = (current) => {
    remainingFieldCharacters = fieldCharacterLimit;
    reserve(256);
    const element = current.element;
    const text = semanticText(element);
    const splitAttributes = attributes(element);
    const recordIndex = nextIndex;
    const record = {
      index: recordIndex,
      tag: element.tagName.toLowerCase(),
      role: normalize(element.getAttribute("role")) || implicitRole(element),
      accessibleName: accessibleName(element, text),
      text,
      id: normalize(element.id),
      attributes: splitAttributes.values,
      urlAttributes: splitAttributes.urls,
      state: state(element),
      selectOptions: options(element),
      shadowDepth: current.shadowDepth,
      parentIndex: current.parentIndex,
      domDepth: current.domDepth,
      hasOpenShadowRoot: Boolean(element.shadowRoot)
    };
    nextIndex += 1;
    enqueueChildren(element, current.shadowDepth, recordIndex, current.domDepth + 1);
    return record;
  };
  const next = (limit) => {
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
      throw new Error("Invalid semantic capture batch limit");
    }
    const records = [];
    let batchBytes = 2;
    while (records.length < limit) {
      const current = pendingRecord ? null : nextElement();
      const record = pendingRecord || (current ? buildRecord(current) : null);
      pendingRecord = null;
      if (!record) break;
      const recordBytes = serializedRecordBytes(record);
      if (recordBytes + 2 > maxBytes) {
        throw new Error("Semantic record exceeds the configured working-set limit");
      }
      const separatorBytes = records.length ? 1 : 0;
      if (records.length && batchBytes + separatorBytes + recordBytes > maxBytes) {
        pendingRecord = record;
        break;
      }
      records.push(record);
      batchBytes += separatorBytes + recordBytes;
    }
    const complete = pendingRecord === null && rootPending === null &&
      cursors.every((cursor) => !cursor.next);
    if (complete && observer) observer.disconnect();
    return {
      records,
      byteCount: batchBytes,
      complete,
      mutated
    };
  };
  return {
    metadata: {
      baseUrl: bounded(document.baseURI),
      contentType: bounded(document.contentType),
      language: document.documentElement ?
        normalize(document.documentElement.lang) : null
    },
    next,
    dispose: () => {
      if (observer) observer.disconnect();
    }
  };
}
"""


@dataclass(slots=True)
class _MetadataBudget:
    limit_bytes: int
    retained_bytes: int = 0

    def retain(self, value: object, *, overhead_bytes: int = 64) -> None:
        payload_bytes = len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        retained_bytes = payload_bytes + overhead_bytes
        if self.retained_bytes + retained_bytes > self.limit_bytes:
            # Operators can raise the byte envelope; item counts remain unrestricted.
            raise SnapshotError("Snapshot metadata exceeds PLANTAIN_SNAPSHOT_WORKING_SET_BYTES")
        self.retained_bytes += retained_bytes


@dataclass(slots=True)
class _Signals:
    form_fields: int = 0
    buttons: int = 0
    selects: int = 0
    links: int = 0
    key_ids: set[str] = field(default_factory=set)

    def observe(
        self,
        records: Sequence[Mapping[str, Any]],
        metadata_budget: _MetadataBudget,
    ) -> None:
        for record in records:
            state = record.get("state")
            if isinstance(state, Mapping) and state.get("visible") is not True:
                continue
            tag = str(record.get("tag") or "")
            role = str(record.get("role") or "")
            attributes = record.get("attributes")
            input_type = (
                str(attributes.get("type") or "").casefold()
                if isinstance(attributes, Mapping)
                else ""
            )
            is_button = (
                tag == "button"
                or role == "button"
                or (tag == "input" and input_type in {"button", "image", "reset", "submit"})
            )
            is_field = (tag in {"input", "textarea"} and not is_button) or role in {
                "checkbox",
                "radio",
                "slider",
                "spinbutton",
                "textbox",
            }
            if is_field:
                self.form_fields += 1
            if is_button:
                self.buttons += 1
            if tag == "select" or role in {"combobox", "listbox"}:
                self.selects += 1
            if tag == "a" or role == "link":
                self.links += 1
            identifier = record.get("id")
            candidate = str(identifier or "")
            if (
                (is_field or is_button)
                and candidate
                and candidate not in self.key_ids
                and not _DYNAMIC_ID.match(candidate)
            ):
                metadata_budget.retain(candidate)
                self.key_ids.add(candidate)

    def counts(self) -> JsonObject:
        return {
            "formFields": self.form_fields,
            "buttons": self.buttons,
            "selects": self.selects,
            "links": self.links,
        }


@dataclass(frozen=True, slots=True)
class _FramePlan:
    frame: Frame
    frame_id: str
    parent_frame_id: str | None


class CompleteSemanticSnapshotExtractor:
    """Capture every discoverable semantic record into a staged v3 bundle."""

    def __init__(
        self,
        *,
        secrets: SecretRegistry,
        snapshots_dir: Path,
        telemetry: NetworkTelemetryRecorder,
        max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
        working_set_bytes: int = DEFAULT_RECORD_CHUNK_BYTES,
    ) -> None:
        if working_set_bytes <= TRANSPORT_PAYLOAD_RESERVE_BYTES:
            raise ValueError("working_set_bytes cannot accommodate transport metadata")
        self._secrets = secrets
        self._snapshots_dir = snapshots_dir
        self._telemetry = telemetry
        self._max_capture_bytes = max_capture_bytes
        self._working_set_bytes = working_set_bytes
        self._transport_payload_bytes = working_set_bytes - TRANSPORT_PAYLOAD_RESERVE_BYTES
        self._network_cursor = telemetry.committed_cursor

    def commit_telemetry(self) -> None:
        self._telemetry.commit(self._network_cursor)

    async def capture(self, page: Page, *, activity: str) -> StagedSnapshot:
        builder = SnapshotBundleBuilder(
            self._snapshots_dir,
            max_capture_bytes=self._max_capture_bytes,
        )
        try:
            initial_url = page.url
            metadata_budget = _MetadataBudget(self._working_set_bytes)
            metadata_budget.retain({}, overhead_bytes=1_024)
            initial_plan = _frame_plan(page, metadata_budget)
            initial_identity = _frame_identity(initial_plan)
            dom_chunks: list[JsonObject] = []
            frames: list[JsonObject] = []
            signals: dict[str, _Signals] = {}
            total_records = 0

            for planned in initial_plan:
                frame_result = await self._capture_frame(
                    builder,
                    planned,
                    metadata_budget,
                )
                dom_chunks.extend(frame_result["domChunks"])
                frames.append(frame_result["frame"])
                metadata_budget.retain(planned.frame_id, overhead_bytes=128)
                signals[planned.frame_id] = frame_result["signals"]
                total_records += frame_result["recordCount"]

            current_plan = _frame_plan(
                page,
                _MetadataBudget(self._working_set_bytes),
            )
            if initial_url != page.url or initial_identity != _frame_identity(current_plan):
                raise SnapshotConsistencyError(  # noqa: TRY301
                    "Page navigation or frame topology changed during capture"
                )

            network_chunks: list[JsonObject] = []
            network_records = 0
            network_start = self._network_cursor
            network_end = self._telemetry.count
            for sequence, records in enumerate(
                self._telemetry.batches(
                    TRANSPORT_RECORDS,
                    max_batch_bytes=self._transport_payload_bytes,
                    start_sequence=network_start,
                    end_sequence=network_end,
                ),
                start=1,
            ):
                descriptor = builder.add_records(
                    kind="network",
                    sequence=sequence,
                    records=records,
                    metadata={},
                    max_payload_bytes=self._working_set_bytes,
                )
                metadata_budget.retain(descriptor)
                network_chunks.append(descriptor)
                network_records += len(records)

            title = self._secrets.redact_text(await page.title())
            safe_url = self._secrets.redact_url(page.url)
            safe_activity = self._secrets.redact_text(activity)
            summary = _structural_summary(safe_url, frames, signals)
            metadata_budget.retain(
                {
                    "activity": safe_activity,
                    "url": safe_url,
                    "pageTitle": title,
                }
            )
            metadata_budget.retain(summary)
            manifest: JsonObject = {
                "schemaVersion": "3.0",
                "captureComplete": True,
                "capturedAt": datetime.now(UTC).isoformat(),
                "activity": safe_activity,
                "url": safe_url,
                "pageTitle": title,
                "frames": frames,
                "content": {
                    "aria": {
                        "format": "semantic-accessibility-records-v1",
                        "source": "dom",
                        "depth": "unlimited",
                        "recordCount": total_records,
                    },
                    "dom": {
                        "format": "semantic-records-v1",
                        "recordCount": total_records,
                        "chunkCount": len(dom_chunks),
                        "chunks": dom_chunks,
                    },
                    "network": {
                        "format": "lifecycle-events-v1",
                        "bodyCapture": False,
                        "fromSequence": network_start,
                        "throughSequence": network_end,
                        "recordCount": network_records,
                        "chunkCount": len(network_chunks),
                        "chunks": network_chunks,
                    },
                },
                "structuralSummary": summary,
            }
            staged = builder.finish(manifest)
            self._network_cursor = network_end
        except SnapshotError:
            builder.abort()
            raise
        except Exception as exc:
            builder.abort()
            raise SnapshotError("Unable to capture a complete semantic snapshot") from exc
        return staged

    async def _capture_frame(
        self,
        builder: SnapshotBundleBuilder,
        planned: _FramePlan,
        metadata_budget: _MetadataBudget,
    ) -> JsonObject:
        frame = planned.frame
        handle = await frame.evaluate_handle(
            _DOM_RECORDS_SCRIPT,
            self._transport_payload_bytes,
        )
        try:
            metadata = await handle.evaluate("capture => capture.metadata")
            if not isinstance(metadata, dict):
                raise SnapshotError("Semantic DOM capture returned an invalid shape")
            dom_chunks: list[JsonObject] = []
            frame_signals = _Signals()
            record_count = 0
            sequence = 0
            while True:
                batch = await handle.evaluate(
                    "(capture, limit) => capture.next(limit)",
                    TRANSPORT_RECORDS,
                )
                records, complete = _validated_dom_batch(
                    batch,
                    self._transport_payload_bytes,
                )
                if complete and not records:
                    break
                sequence += 1
                sanitized = self._sanitize_records(records)
                frame_signals.observe(sanitized, metadata_budget)
                descriptor = builder.add_records(
                    kind="dom",
                    sequence=sequence,
                    records=sanitized,
                    metadata={"frameId": planned.frame_id},
                    max_payload_bytes=self._working_set_bytes,
                )
                metadata_budget.retain(descriptor)
                dom_chunks.append(descriptor)
                record_count += len(records)
                if complete:
                    break
        finally:
            try:
                await handle.evaluate("capture => capture.dispose()")
            finally:
                await handle.dispose()

        safe_metadata = self._secrets.redact(metadata)
        if not isinstance(safe_metadata, dict):
            raise SnapshotError("Frame metadata redaction returned an invalid shape")
        base_url = safe_metadata.get("baseUrl")
        if isinstance(base_url, str):
            safe_metadata["baseUrl"] = self._secrets.redact_url(base_url)
        frame_document: JsonObject = {
            "frameId": planned.frame_id,
            "parentFrameId": planned.parent_frame_id,
            "name": self._secrets.redact_text(frame.name or ""),
            "url": self._secrets.redact_url(frame.url or "about:blank"),
            "metadata": safe_metadata,
            "recordCount": record_count,
        }
        metadata_budget.retain(frame_document)
        return {
            "domChunks": dom_chunks,
            "recordCount": record_count,
            "signals": frame_signals,
            "frame": frame_document,
        }

    def _sanitize_records(self, records: list[JsonObject]) -> list[JsonObject]:
        for record in records:
            urls = record.get("urlAttributes")
            if isinstance(urls, dict):
                for name, value in urls.items():
                    if isinstance(value, str):
                        urls[name] = self._secrets.redact_url(value)
        sanitized = self._secrets.redact(records)
        if not isinstance(sanitized, list) or not all(
            isinstance(record, dict) for record in sanitized
        ):
            raise SnapshotError("Semantic DOM redaction returned invalid records")
        return sanitized


def _validated_dom_batch(
    batch: object,
    max_batch_bytes: int,
) -> tuple[list[JsonObject], bool]:
    if not isinstance(batch, dict):
        raise SnapshotError("Semantic DOM transport returned an invalid batch")
    records = batch.get("records")
    byte_count = batch.get("byteCount")
    complete = batch.get("complete")
    mutated = batch.get("mutated")
    if (
        not isinstance(records, list)
        or not all(isinstance(record, dict) for record in records)
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < MIN_JSON_ARRAY_BYTES
        or not isinstance(complete, bool)
        or not isinstance(mutated, bool)
    ):
        raise SnapshotError("Semantic DOM transport returned invalid records")
    if byte_count > max_batch_bytes:
        raise SnapshotError("Semantic DOM batch exceeds PLANTAIN_SNAPSHOT_WORKING_SET_BYTES")
    if mutated:
        raise SnapshotConsistencyError("DOM changed during semantic capture")
    if not records and not complete:
        raise SnapshotError("Semantic DOM transport made no progress")
    return records, complete


def _frame_plan(
    page: Page,
    metadata_budget: _MetadataBudget,
) -> list[_FramePlan]:
    planned: list[_FramePlan] = []
    pending: list[_FramePlan] = [_FramePlan(page.main_frame, "main", None)]
    while pending:
        current = pending.pop()
        metadata_budget.retain(
            {
                "frameId": current.frame_id,
                "parentFrameId": current.parent_frame_id,
            },
            overhead_bytes=128,
        )
        planned.append(current)
        children = current.frame.child_frames
        pending.extend(
            _FramePlan(children[index], f"{current.frame_id}.{index}", current.frame_id)
            for index in range(len(children) - 1, -1, -1)
        )
    return planned


def _frame_identity(planned: Sequence[_FramePlan]) -> str:
    digest = hashlib.sha256()
    for item in planned:
        digest.update(str(id(item.frame)).encode("ascii"))
        digest.update(b"\0")
        digest.update(item.frame_id.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _structural_summary(
    page_url: str,
    frames: Sequence[Mapping[str, Any]],
    signals: Mapping[str, _Signals],
) -> JsonObject:
    main = signals.get("main", _Signals())
    nested = []
    for frame in frames:
        frame_id = str(frame.get("frameId") or "")
        if frame_id == "main":
            continue
        frame_url = str(frame.get("url") or "about:blank")
        frame_signals = signals.get(frame_id, _Signals())
        nested.append(
            {
                "frameId": frame_id,
                "parentFrameId": frame.get("parentFrameId"),
                "frameUrl": frame_url,
                "normalizedFrameUrl": normalized_url_key(frame_url),
                "elementCounts": frame_signals.counts(),
                "nestedKeyIds": sorted(frame_signals.key_ids),
            }
        )
    first_frame_url = str(nested[0]["frameUrl"]) if nested else "about:blank"
    return {
        "normalizedKey": normalized_url_key(page_url),
        "normalizedFrameUrl": normalized_url_key(first_frame_url),
        "elementCounts": main.counts(),
        "keyIds": sorted(main.key_ids),
        "nestedElementCounts": nested,
    }


def normalized_url_key(url: str) -> str:
    if url == "about:blank":
        return url
    parts = urlsplit(url)
    path = _PORTAL_BUILD.sub("/", parts.path or "/")
    host = (parts.hostname or "unknown").lower()
    return f"{host}::{path}"


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# Boolean values are intentionally not valid record counts.
# Cursor byte counts follow the same strict integer rule.
# Batch byte ceilings are checked before redaction and again before persistence.
