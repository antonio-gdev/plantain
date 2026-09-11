"""Adversarial coverage for bounded content-addressed OpenAPI caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.api import openapi_cache
from plantain.activities.api.client import ApiActivityError, BoundedResponse
from plantain.activities.api.openapi import OpenApiStore
from plantain.activities.api.openapi_cache import (
    CacheBinding,
    OpenApiCache,
    canonical_document,
)
from plantain.models.api import HttpMethod

OPENAPI_MAX_BYTES = 2_000_000
HTTP_OK = 200
HTTP_NOT_MODIFIED = 304


class FakeApi:
    """Deterministic bounded-response queue."""

    def __init__(self, responses: list[BoundedResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> BoundedResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _settings(tmp_path: Path) -> Any:
    return SimpleNamespace(
        generated_dir=tmp_path / "generated",
        openapi_max_bytes=OPENAPI_MAX_BYTES,
    )


def _document(*, title: str = "Example") -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": title, "version": "1.0"},
        "servers": [{"url": "https://api.example.test"}],
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def _response(
    status: int,
    body: Any,
    *,
    request_url: str,
    etag: str | None = None,
) -> BoundedResponse:
    headers = {"content-type": "application/json"}
    if etag is not None:
        headers["etag"] = etag
    content = b"" if body is None else json.dumps(body).encode("utf-8")
    return BoundedResponse(
        request_url=request_url,
        method=HttpMethod.GET,
        status_code=status,
        headers=headers,
        content=content,
        elapsed_ms=1,
    )


def _cache_document(
    cache: OpenApiCache,
    document: dict[str, Any],
    source_url: str,
) -> tuple[str, Path]:
    canonical = canonical_document(document, max_bytes=OPENAPI_MAX_BYTES)
    schema_id = hashlib.sha256(canonical).hexdigest()
    target = cache.persist(
        schema_id,
        canonical,
        CacheBinding(source_url=source_url, request_identity=None),
    )
    return schema_id, target


def test_cached_document_tampering_is_detected(tmp_path: Path) -> None:
    source_url = "https://schema.example.test/openapi.json"
    api = FakeApi([_response(HTTP_OK, _document(), request_url=source_url)])
    store = OpenApiStore(_settings(tmp_path), cast("Any", api))

    loaded = asyncio.run(store.load_url(source_url))
    loaded.cache_file.write_text(
        json.dumps(_document(title="Tampered")),
        encoding="utf-8",
    )

    with pytest.raises(ApiActivityError, match="does not match its content digest"):
        asyncio.run(store.load_id(loaded.schema_id))


def test_digest_valid_cached_document_is_fully_revalidated(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    cache = OpenApiCache(
        settings.generated_dir / "openapi",
        max_document_bytes=OPENAPI_MAX_BYTES,
    )
    document = _document()
    document["components"] = {
        "schemas": {"Unsafe": {"$ref": "https://internal.example.test/schema.json"}}
    }
    schema_id, _target = _cache_document(
        cache,
        document,
        "https://schema.example.test/openapi.json",
    )
    store = OpenApiStore(settings, cast("Any", FakeApi([])))

    with pytest.raises(ApiActivityError, match="External OpenAPI references are blocked"):
        asyncio.run(store.load_id(schema_id))


def test_same_document_from_two_origins_requires_url_context(tmp_path: Path) -> None:
    first_source = "https://one.example.test/openapi.json"
    second_source = "https://two.example.test/openapi.json"
    api = FakeApi(
        [
            _response(HTTP_OK, _document(), request_url=first_source),
            _response(HTTP_OK, _document(), request_url=second_source),
        ]
    )
    store = OpenApiStore(_settings(tmp_path), cast("Any", api))

    first = asyncio.run(store.load_url(first_source))
    second = asyncio.run(store.load_url(second_source))

    assert first.schema_id == second.schema_id
    assert first.source_url == first_source
    assert second.source_url == second_source
    with pytest.raises(ApiActivityError, match="ambiguous source metadata"):
        asyncio.run(store.load_id(first.schema_id))


@pytest.mark.parametrize("failure_mode", ["missing", "tampered"])
def test_not_modified_with_unusable_cache_refetches_without_validators(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    source_url = "https://schema.example.test/openapi.json"
    api = FakeApi(
        [
            _response(HTTP_OK, _document(), request_url=source_url, etag='"v1"'),
            _response(HTTP_NOT_MODIFIED, None, request_url=source_url),
            _response(HTTP_OK, _document(), request_url=source_url, etag='"v1"'),
        ]
    )
    store = OpenApiStore(_settings(tmp_path), cast("Any", api))
    first = asyncio.run(store.load_url(source_url))
    if failure_mode == "missing":
        first.cache_file.unlink()
    else:
        first.cache_file.write_text("{}", encoding="utf-8")

    recovered = asyncio.run(store.load_url(source_url))

    assert recovered.schema_id == first.schema_id
    assert api.calls[1]["headers"] == {"If-None-Match": '"v1"'}
    assert api.calls[2]["headers"] == {}


def test_query_bearing_schema_url_is_not_conditionally_cached(tmp_path: Path) -> None:
    query_marker = "synthetic-query-marker"
    source_url = f"https://schema.example.test/openapi.json?variant={query_marker}"
    api = FakeApi(
        [
            _response(HTTP_OK, _document(), request_url=source_url, etag='"v1"'),
            _response(HTTP_OK, _document(), request_url=source_url, etag='"v1"'),
        ]
    )
    store = OpenApiStore(_settings(tmp_path), cast("Any", api))

    asyncio.run(store.load_url(source_url))
    asyncio.run(store.load_url(source_url))

    assert api.calls[0]["headers"] == {}
    assert api.calls[1]["headers"] == {}
    index = store._read_index()
    assert index["requests"] == {}
    assert query_marker not in store._index_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("limit_name", "message"),
    [
        ("MAX_CACHE_ENTRIES", "source capacity"),
        ("MAX_CACHED_DOCUMENTS", "document capacity"),
    ],
)
def test_cache_capacity_is_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    message: str,
) -> None:
    monkeypatch.setattr(openapi_cache, limit_name, 1)
    cache = OpenApiCache(
        tmp_path / "openapi",
        max_document_bytes=OPENAPI_MAX_BYTES,
    )
    _cache_document(cache, _document(title="First"), "https://one.example.test/schema")

    with pytest.raises(ApiActivityError, match=message):
        _cache_document(
            cache,
            _document(title="Second"),
            "https://two.example.test/schema",
        )


def test_cached_document_read_is_byte_bounded(tmp_path: Path) -> None:
    cache = OpenApiCache(tmp_path / "openapi", max_document_bytes=8)
    cache.ensure()
    schema_id = "a" * 64
    (cache.root / f"{schema_id}.json").write_bytes(b'{"openapi":"3.0.3"}')

    with pytest.raises(ApiActivityError, match="exceeds its byte limit"):
        cache.load_document(schema_id)


def test_cache_index_read_is_byte_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_limit = 16
    monkeypatch.setattr(openapi_cache, "MAX_CACHE_INDEX_BYTES", index_limit)
    cache = OpenApiCache(
        tmp_path / "openapi",
        max_document_bytes=OPENAPI_MAX_BYTES,
    )
    cache.ensure()
    cache.index_path.write_bytes(b"x" * (index_limit + 1))

    with pytest.raises(ApiActivityError, match="index exceeds its byte limit"):
        cache.read_index()


def test_cache_reads_reject_symlinked_files(tmp_path: Path) -> None:
    cache = OpenApiCache(
        tmp_path / "openapi",
        max_document_bytes=OPENAPI_MAX_BYTES,
    )
    cache.ensure()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}")
    cache.index_path.symlink_to(outside)

    with pytest.raises(ApiActivityError, match="cache index is invalid"):
        cache.read_index()

    cache.index_path.unlink()
    schema_id = "a" * 64
    target = cache.root / f"{schema_id}.json"
    target.symlink_to(outside)
    with pytest.raises(ApiActivityError, match=r"Cached OpenAPI schema.*is invalid"):
        cache._read_document(target, schema_id)
