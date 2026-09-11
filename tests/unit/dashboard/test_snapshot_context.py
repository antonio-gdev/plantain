"""Verified snapshot selection and redaction coverage for dashboard agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from plantain.activities.snapshot_registry_complete import (
    VerifiedSnapshotExcerpt,
    VerifiedSnapshotSummary,
)
from plantain.activities.ui_errors import SnapshotError
from plantain.dashboard import snapshot_context
from plantain.dashboard.snapshot_context import (
    SnapshotContextError,
    select_verified_snapshot_context,
)
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

EXPECTED_STATE_COUNT = 2
SMALL_EXCERPT_BYTES = 5


@dataclass(slots=True)
class _Registry:
    summaries: tuple[VerifiedSnapshotSummary, ...]
    excerpts: dict[str, VerifiedSnapshotExcerpt]
    reads: list[tuple[str, int]] = field(default_factory=list)

    def verified_summaries(self) -> tuple[VerifiedSnapshotSummary, ...]:
        return self.summaries

    def read_verified_excerpt(
        self,
        canonical_file: str,
        *,
        max_bytes: int,
    ) -> VerifiedSnapshotExcerpt:
        self.reads.append((canonical_file, max_bytes))
        stored = self.excerpts[canonical_file]
        content, truncated = _bounded_utf8(stored.content, max_bytes)
        return VerifiedSnapshotExcerpt(
            summary=stored.summary,
            content=content,
            truncated=stored.truncated or truncated,
        )


class _FailingRegistry:
    def verified_summaries(self) -> tuple[VerifiedSnapshotSummary, ...]:
        raise SnapshotError("private snapshot detail")

    def read_verified_excerpt(
        self,
        canonical_file: str,
        *,
        max_bytes: int,
    ) -> VerifiedSnapshotExcerpt:
        raise AssertionError((canonical_file, max_bytes))


def _summary(
    canonical_file: str,
    *,
    structural_digest: str,
    last_updated: str,
    url: str = "https://example.test/checkout",
    page_title: str = "Checkout",
    normalized_key: str = "example.test/checkout",
    key_ids: tuple[str, ...] = ("checkout-button",),
) -> VerifiedSnapshotSummary:
    return VerifiedSnapshotSummary(
        canonical_file=canonical_file,
        url=url,
        page_title=page_title,
        activities=("checkout",),
        normalized_key=normalized_key,
        structural_digest=structural_digest,
        element_counts=(("button", 1),),
        key_ids=key_ids,
        last_updated=last_updated,
    )


def _excerpt(
    summary: VerifiedSnapshotSummary,
    content: str = '- button "Checkout"\n',
) -> VerifiedSnapshotExcerpt:
    return VerifiedSnapshotExcerpt(
        summary=summary,
        content=content,
    )


def _bounded_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value, False
    prefix = encoded[:limit]
    while prefix:
        try:
            return prefix.decode(), True
        except UnicodeDecodeError as exc:
            prefix = prefix[: exc.start]
    return "", True


def test_selection_keeps_latest_duplicate_and_distinct_structural_states(
    tmp_path: Path,
) -> None:
    older = _summary(
        "checkout-old.semantic.json",
        structural_digest="same-checkout-state",
        last_updated="2026-08-01T00:00:00+00:00",
    )
    latest = _summary(
        "checkout-new.semantic.json",
        structural_digest="same-checkout-state",
        last_updated="2026-08-03T00:00:00+00:00",
    )
    cart = _summary(
        "cart.semantic.json",
        structural_digest="distinct-cart-state",
        last_updated="2026-08-02T00:00:00+00:00",
        page_title="Checkout cart",
    )
    registry = _Registry(
        summaries=(older, cart, latest),
        excerpts={
            latest.canonical_file: _excerpt(latest),
            cart.canonical_file: _excerpt(cart, '- region "Cart"\n'),
        },
    )

    packet = select_verified_snapshot_context(
        tmp_path,
        "Analyze the checkout states.",
        SecretRegistry(),
        registry=registry,
    )

    assert packet.source_count == EXPECTED_STATE_COUNT
    assert [item.canonical_file for item in packet.excerpts] == [
        latest.canonical_file,
        cart.canonical_file,
    ]
    assert [item[0] for item in registry.reads] == [
        latest.canonical_file,
        cart.canonical_file,
    ]
    assert older.canonical_file not in {item[0] for item in registry.reads}
    assert packet.selection_limited is True


def test_selection_redacts_every_serialized_snapshot_surface(
    tmp_path: Path,
) -> None:
    sensitive_value = "snapshot-private-token"
    summary = _summary(
        f"{sensitive_value}.semantic.json",
        structural_digest="redacted-state",
        last_updated="2026-08-03T00:00:00+00:00",
        url=f"https://example.test/checkout?token={sensitive_value}",
        page_title=f"Checkout {sensitive_value}",
        normalized_key=f"example.test/{sensitive_value}",
        key_ids=(f"button-{sensitive_value}",),
    )
    registry = _Registry(
        summaries=(summary,),
        excerpts={
            summary.canonical_file: _excerpt(
                summary,
                f'- textbox "token" value="{sensitive_value}"\n',
            )
        },
    )
    secrets = SecretRegistry()
    secrets.observe_environment("CONTEXT_API_TOKEN", sensitive_value)

    packet = select_verified_snapshot_context(
        tmp_path,
        "Analyze checkout.",
        secrets,
        registry=registry,
    )

    serialized = packet.model_dump_json()
    assert sensitive_value not in serialized
    assert REDACTED in serialized
    assert packet.excerpts[0].evidence_id.startswith("snapshot-")


def test_selection_enforces_utf8_safe_per_excerpt_and_total_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _summary(
        "unicode.semantic.json",
        structural_digest="unicode-state",
        last_updated="2026-08-03T00:00:00+00:00",
    )
    registry = _Registry(
        summaries=(summary,),
        excerpts={summary.canonical_file: _excerpt(summary, "ééé")},
    )
    monkeypatch.setattr(
        snapshot_context,
        "MAX_SNAPSHOT_CONTEXT_EXCERPT_BYTES",
        SMALL_EXCERPT_BYTES,
    )
    monkeypatch.setattr(
        snapshot_context,
        "MAX_SNAPSHOT_CONTEXT_TOTAL_BYTES",
        SMALL_EXCERPT_BYTES,
    )

    packet = select_verified_snapshot_context(
        tmp_path,
        "Analyze Unicode evidence.",
        SecretRegistry(),
        registry=registry,
    )

    assert registry.reads == [(summary.canonical_file, SMALL_EXCERPT_BYTES)]
    assert packet.excerpts[0].content == "éé"
    assert len(packet.excerpts[0].content.encode()) <= SMALL_EXCERPT_BYTES
    assert packet.excerpts[0].truncated is True
    assert packet.selection_limited is True


def test_selection_translates_registry_failures_without_private_detail(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        SnapshotContextError,
        match="could not be prepared safely",
    ) as captured:
        select_verified_snapshot_context(
            tmp_path,
            "Analyze checkout.",
            SecretRegistry(),
            registry=_FailingRegistry(),
        )

    assert "private snapshot detail" not in str(captured.value)
