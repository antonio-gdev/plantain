"""Tests for the human-maintained sensitive field-name file."""

from __future__ import annotations

from pathlib import Path

import pytest

from plantain.config import _load_sensitive_key_names
from plantain.errors import ConfigurationError


def test_loader_ignores_comments_and_normalizes_unique_names(tmp_path: Path) -> None:
    configured = tmp_path / "sensitive-keys.txt"
    configured.write_text(
        "# Field names only\nIP_ADDRESS\nip_address\nFIRST_PASSWORD\n",
        encoding="utf-8",
    )

    assert _load_sensitive_key_names(tmp_path) == ("first_password", "ip_address")


def test_loader_rejects_an_oversized_entry_without_echoing_it(tmp_path: Path) -> None:
    configured = tmp_path / "sensitive-keys.txt"
    configured.write_text("x" * 257, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=r"invalid entry at line 1"):
        _load_sensitive_key_names(tmp_path)


def test_loader_returns_empty_policy_when_file_is_absent(tmp_path: Path) -> None:
    assert _load_sensitive_key_names(tmp_path) == ()
