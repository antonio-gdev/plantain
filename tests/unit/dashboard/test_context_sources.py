"""Private dashboard context-source indexing and persistence coverage."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from plantain.dashboard import context_selection, context_sources
from plantain.dashboard.context_sources import (
    ContextSourceError,
    ContextSourceKind,
    add_context_source,
    load_context_catalog,
    remove_context_source,
)
from plantain.persistence import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    write_bytes_atomic,
    write_json_atomic,
)

EXPECTED_INDEXED_FILES = 2
SMALL_MANIFEST_LIMIT = 16


def _manifest_path(root: Path) -> Path:
    return root / ".plantain/dashboard/context-sources.json"


def _stored_manifest(root: Path) -> dict[str, object]:
    return json.loads(_manifest_path(root).read_text(encoding="utf-8"))


def test_application_source_filters_unsafe_and_generated_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "application"
    source.mkdir()
    (source / "src").mkdir()
    (source / "node_modules").mkdir()
    (source / "output").mkdir()
    (source / "README.md").write_text("Business rules", encoding="utf-8")
    (source / "src/app.py").write_text("def run(): ...\n", encoding="utf-8")
    (source / ".env").write_text("DO_NOT_READ=this", encoding="utf-8")
    (source / "private.key").write_text("DO_NOT_READ=this", encoding="utf-8")
    (source / "image.png").write_bytes(b"\x89PNG")
    (source / "node_modules/dependency.js").write_text("ignored", encoding="utf-8")
    (source / "output/result.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("ignored = True\n", encoding="utf-8")
    (source / "linked.py").symlink_to(outside)

    catalog = add_context_source(
        tmp_path,
        ContextSourceKind.APPLICATION,
        "application",
    )

    assert catalog.application_source_available is True
    assert catalog.requirements_available is False
    assert len(catalog.sources) == 1
    summary = catalog.sources[0]
    assert summary.label == "application"
    assert summary.indexed_file_count == EXPECTED_INDEXED_FILES
    assert summary.partial is False
    assert summary.available is True
    assert not hasattr(summary, "path")

    stored = _stored_manifest(tmp_path)
    assert stored["schemaVersion"] == "1.0"
    sources = stored["sources"]
    assert isinstance(sources, list)
    assert sources[0]["files"] == ["README.md", "src/app.py"]
    assert stat.S_IMODE(_manifest_path(tmp_path).stat().st_mode) == PRIVATE_FILE_MODE
    assert stat.S_IMODE((tmp_path / ".plantain/dashboard").stat().st_mode) == PRIVATE_DIRECTORY_MODE


def test_external_source_is_allowed_and_reindexing_is_idempotent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external-project"
    source.mkdir()
    (source / "requirements.md").write_text("A user can sign in.", encoding="utf-8")

    first = add_context_source(
        workspace,
        ContextSourceKind.REQUIREMENTS,
        str(source),
    )
    (source / "checkout.feature").write_text(
        "Feature: checkout\n",
        encoding="utf-8",
    )
    second = add_context_source(
        workspace,
        ContextSourceKind.REQUIREMENTS,
        str(source),
    )

    assert len(first.sources) == 1
    assert len(second.sources) == 1
    assert first.sources[0].source_id == second.sources[0].source_id
    assert second.sources[0].indexed_file_count == EXPECTED_INDEXED_FILES
    assert second.requirements_available is True


def test_missing_source_is_visible_and_removal_never_deletes_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "contract.yaml"
    source.write_text("openapi: 3.1.0\n", encoding="utf-8")
    catalog = add_context_source(
        tmp_path,
        ContextSourceKind.API_CONTRACT,
        str(source),
    )
    source_id = catalog.sources[0].source_id

    source.unlink()
    missing = load_context_catalog(tmp_path)

    assert missing.api_schema_available is False
    assert missing.sources[0].available is False
    assert missing.notice == "1 attached source is unavailable."

    source.write_text("openapi: 3.1.0\n", encoding="utf-8")
    removed = remove_context_source(tmp_path, source_id)

    assert removed.sources == ()
    assert source.exists()
    with pytest.raises(ContextSourceError, match="no longer exists"):
        remove_context_source(tmp_path, source_id)


def test_source_roots_and_parent_components_must_not_be_symlinks(
    tmp_path: Path,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "rules.md").write_text("Rules", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ContextSourceError, match="Symbolic links"):
        add_context_source(
            tmp_path,
            ContextSourceKind.REQUIREMENTS,
            str(linked),
        )

    catalog = add_context_source(
        tmp_path,
        ContextSourceKind.REQUIREMENTS,
        str(actual),
    )
    relocated = tmp_path / "relocated"
    actual.rename(relocated)
    actual.symlink_to(relocated, target_is_directory=True)

    refreshed = load_context_catalog(tmp_path)

    assert catalog.sources[0].available is True
    assert refreshed.sources[0].available is False


def test_large_source_is_partially_indexed_with_visible_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "large"
    source.mkdir()
    (source / "one.py").write_text("one = 1\n", encoding="utf-8")
    (source / "two.py").write_text("two = 2\n", encoding="utf-8")
    monkeypatch.setattr(
        context_sources,
        "MAX_INDEXED_FILES_PER_SOURCE",
        1,
    )

    catalog = add_context_source(
        tmp_path,
        ContextSourceKind.APPLICATION,
        str(source),
    )

    assert catalog.sources[0].indexed_file_count == 1
    assert catalog.sources[0].partial is True
    assert "partially indexed" in catalog.notice


def test_source_count_and_identifiers_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    monkeypatch.setattr(context_sources, "MAX_STORED_CONTEXT_SOURCES", 1)
    add_context_source(
        tmp_path,
        ContextSourceKind.REQUIREMENTS,
        str(first),
    )

    with pytest.raises(ContextSourceError, match="catalog is full"):
        add_context_source(
            tmp_path,
            ContextSourceKind.REQUIREMENTS,
            str(second),
        )
    with pytest.raises(ContextSourceError, match="identifier is invalid"):
        remove_context_source(tmp_path, "../context-sources.json")
    assert len(load_context_catalog(tmp_path).sources) == 1


def test_catalog_capacity_is_independent_of_agent_excerpt_budget(
    tmp_path: Path,
) -> None:
    attachment_count = context_selection.MAX_AGENT_CONTEXT_EXCERPTS + 1
    for index in range(attachment_count):
        source = tmp_path / f"requirement-{index}.md"
        source.write_text(f"Requirement {index}", encoding="utf-8")
        add_context_source(
            tmp_path,
            ContextSourceKind.REQUIREMENTS,
            str(source),
        )

    assert len(load_context_catalog(tmp_path).sources) == attachment_count


def test_manifest_capacity_failure_preserves_prior_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("First", encoding="utf-8")
    second.write_text("Second", encoding="utf-8")
    add_context_source(tmp_path, ContextSourceKind.REQUIREMENTS, str(first))
    manifest = _manifest_path(tmp_path)
    monkeypatch.setattr(
        context_sources,
        "CONTEXT_MANIFEST_MAX_BYTES",
        manifest.stat().st_size,
    )

    with pytest.raises(ContextSourceError, match="local capacity"):
        add_context_source(tmp_path, ContextSourceKind.REQUIREMENTS, str(second))

    catalog = load_context_catalog(tmp_path)
    assert [source.label for source in catalog.sources] == ["first.md"]


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"schemaVersion":"2.0","sources":[]}',
        b'{"schemaVersion":"1.0","sources":"invalid"}',
    ],
)
def test_malformed_manifest_is_rejected(
    tmp_path: Path,
    payload: bytes,
) -> None:
    write_bytes_atomic(_manifest_path(tmp_path), payload)

    with pytest.raises(ContextSourceError, match="metadata is invalid"):
        load_context_catalog(tmp_path)


def test_oversized_manifest_and_unsupported_source_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = tmp_path / "archive.bin"
    unsupported.write_bytes(b"binary")
    with pytest.raises(ContextSourceError, match="supported text"):
        add_context_source(
            tmp_path,
            ContextSourceKind.APPLICATION,
            str(unsupported),
        )

    monkeypatch.setattr(
        context_sources,
        "CONTEXT_MANIFEST_MAX_BYTES",
        SMALL_MANIFEST_LIMIT,
    )
    write_json_atomic(
        _manifest_path(tmp_path),
        {"schemaVersion": "1.0", "sources": []},
    )
    with pytest.raises(ContextSourceError, match="byte limit"):
        load_context_catalog(tmp_path)
