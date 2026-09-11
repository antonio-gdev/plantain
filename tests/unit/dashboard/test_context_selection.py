"""Intent-relevant and redacted dashboard context-selection coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from plantain.dashboard import context_selection
from plantain.dashboard.context_selection import select_agent_context
from plantain.dashboard.context_sources import (
    ContextSourceKind,
    add_context_source,
)
from plantain.security.redaction import REDACTED
from plantain.security.secrets import SecretRegistry

EXPECTED_SOURCE_COUNT = 3
EXPECTED_BUDGET_SOURCE_COUNT = 2
SMALL_EXCERPT_BYTES = 5
SMALL_TOTAL_BYTES = 10
SINGLE_EXCERPT = 1


def test_selection_combines_evidence_in_priority_and_intent_order(
    tmp_path: Path,
) -> None:
    application = tmp_path / "application"
    application.mkdir()
    (application / "checkout_service.py").write_text(
        "def complete_checkout():\n    return 'completed'\n",
        encoding="utf-8",
    )
    (application / "profile.py").write_text(
        "def profile():\n    return 'profile'\n",
        encoding="utf-8",
    )
    requirements = tmp_path / "checkout-requirements.md"
    requirements.write_text(
        "Checkout requires an in-stock product.",
        encoding="utf-8",
    )
    contract = tmp_path / "openapi.yaml"
    contract.write_text(
        "openapi: 3.1.0\ninfo:\n  title: Checkout\n  version: 1.0.0\n",
        encoding="utf-8",
    )
    add_context_source(
        tmp_path,
        ContextSourceKind.APPLICATION,
        str(application),
    )
    add_context_source(
        tmp_path,
        ContextSourceKind.REQUIREMENTS,
        str(requirements),
    )
    add_context_source(
        tmp_path,
        ContextSourceKind.API_CONTRACT,
        str(contract),
    )

    selection = select_agent_context(
        tmp_path,
        "Validate the checkout API against its business requirements.",
        SecretRegistry(),
    )

    packet = selection.packet
    assert packet.source_count == EXPECTED_SOURCE_COUNT
    assert selection.available_kinds == frozenset(ContextSourceKind)
    assert packet.excerpts[0].source_kind is ContextSourceKind.REQUIREMENTS
    assert packet.excerpts[1].source_kind is ContextSourceKind.API_CONTRACT
    assert packet.excerpts[2].source_kind is ContextSourceKind.APPLICATION
    assert packet.excerpts[2].relative_path == "checkout_service.py"
    serialized = packet.model_dump_json()
    assert str(tmp_path) not in serialized
    assert "in-stock product" in serialized
    assert "complete_checkout" in serialized
    assert "openapi" in serialized


def test_selection_redacts_observed_values_and_never_reads_dotenv(
    tmp_path: Path,
) -> None:
    source = tmp_path / "requirements"
    source.mkdir()
    sensitive_value = "context-secret-value"
    (source / "rules.md").write_text(
        f"The private marker is {sensitive_value}.",
        encoding="utf-8",
    )
    (source / ".env").write_text(
        "FORBIDDEN_DOTENV_MARKER=visible-only-if-read",
        encoding="utf-8",
    )
    add_context_source(
        tmp_path,
        ContextSourceKind.REQUIREMENTS,
        str(source),
    )
    secrets = SecretRegistry()
    secrets.observe_environment("CONTEXT_API_TOKEN", sensitive_value)

    packet = select_agent_context(
        tmp_path,
        "Use the requirements.",
        secrets,
    ).packet

    serialized = packet.model_dump_json()
    assert sensitive_value not in serialized
    assert REDACTED in serialized
    assert "FORBIDDEN_DOTENV_MARKER" not in serialized


def test_selection_truncates_on_a_valid_utf8_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "unicode.txt"
    source.write_text("ééé", encoding="utf-8")
    add_context_source(
        tmp_path,
        ContextSourceKind.REQUIREMENTS,
        str(source),
    )
    monkeypatch.setattr(
        context_selection,
        "MAX_AGENT_CONTEXT_EXCERPT_BYTES",
        SMALL_EXCERPT_BYTES,
    )
    monkeypatch.setattr(
        context_selection,
        "MAX_AGENT_CONTEXT_TOTAL_BYTES",
        SMALL_EXCERPT_BYTES,
    )

    packet = select_agent_context(
        tmp_path,
        "Read the Unicode requirement.",
        SecretRegistry(),
    ).packet

    assert packet.selection_limited is True
    assert packet.excerpts[0].content == "éé"
    assert packet.excerpts[0].truncated is True
    assert len(packet.excerpts[0].content.encode()) <= SMALL_EXCERPT_BYTES


def test_selection_enforces_one_total_byte_budget_across_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("abcdefgh", encoding="utf-8")
    second.write_text("ijklmnop", encoding="utf-8")
    add_context_source(
        tmp_path,
        ContextSourceKind.REQUIREMENTS,
        str(first),
    )
    add_context_source(
        tmp_path,
        ContextSourceKind.APPLICATION,
        str(second),
    )
    monkeypatch.setattr(
        context_selection,
        "MAX_AGENT_CONTEXT_EXCERPT_BYTES",
        8,
    )
    monkeypatch.setattr(
        context_selection,
        "MAX_AGENT_CONTEXT_TOTAL_BYTES",
        SMALL_TOTAL_BYTES,
    )

    packet = select_agent_context(
        tmp_path,
        "Compare both sources.",
        SecretRegistry(),
    ).packet

    retained_bytes = sum(len(item.content.encode()) for item in packet.excerpts)
    assert retained_bytes <= SMALL_TOTAL_BYTES
    assert packet.source_count == EXPECTED_BUDGET_SOURCE_COUNT
    assert packet.selection_limited is True


def test_selection_excerpt_limit_is_independent_of_catalog_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("application.py", "requirements.md"):
        source = tmp_path / name
        source.write_text(f"Relevant content from {name}", encoding="utf-8")
        add_context_source(
            tmp_path,
            ContextSourceKind.APPLICATION,
            str(source),
        )
    monkeypatch.setattr(
        context_selection,
        "MAX_AGENT_CONTEXT_EXCERPTS",
        SINGLE_EXCERPT,
    )

    packet = select_agent_context(
        tmp_path,
        "Use all relevant application context.",
        SecretRegistry(),
    ).packet

    assert len(packet.excerpts) == SINGLE_EXCERPT
    assert packet.source_count == SINGLE_EXCERPT
    assert packet.selection_limited is True


def test_binary_content_and_late_symlinks_fail_closed(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"header\0payload")
    add_context_source(
        tmp_path,
        ContextSourceKind.REQUIREMENTS,
        str(binary),
    )

    binary_packet = select_agent_context(
        tmp_path,
        "Read the requirement.",
        SecretRegistry(),
    ).packet

    assert binary_packet.excerpts == []
    assert binary_packet.source_count == 0
    assert binary_packet.selection_limited is True

    source = tmp_path / "application.py"
    source.write_text("safe = True\n", encoding="utf-8")
    add_context_source(
        tmp_path,
        ContextSourceKind.APPLICATION,
        str(source),
    )
    moved = tmp_path / "moved.py"
    source.rename(moved)
    source.symlink_to(moved)

    swapped = select_agent_context(
        tmp_path,
        "Inspect application.py.",
        SecretRegistry(),
    )

    assert all(
        excerpt.source_kind is not ContextSourceKind.APPLICATION
        for excerpt in swapped.packet.excerpts
    )
    assert swapped.packet.selection_limited is True
