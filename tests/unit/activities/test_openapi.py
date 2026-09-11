"""On-demand Swagger/OpenAPI loading and operation-resolution tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.api.client import ApiActivityError, BoundedResponse
from plantain.activities.api.openapi import OpenApiStore, _validate_document
from plantain.models.api import HttpMethod
from plantain.persistence import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE

SCHEMA_ID_LENGTH = 64


class FakeApi:
    def __init__(self, responses: list[BoundedResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> BoundedResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _bounded(status: int, body: Any, *, etag: str | None = None) -> BoundedResponse:
    headers = {"content-type": "application/json"}
    if etag is not None:
        headers["etag"] = etag
    content = b"" if body is None else json.dumps(body).encode()
    return BoundedResponse(
        request_url="https://petstore.swagger.io/v2/swagger.json",
        method=HttpMethod.GET,
        status_code=status,
        headers=headers,
        content=content,
        elapsed_ms=2,
    )


def _petstore_document() -> dict[str, Any]:
    return {
        "swagger": "2.0",
        "info": {"title": "Petstore", "version": "1.0"},
        "host": "petstore.swagger.io",
        "basePath": "/v2",
        "schemes": ["https", "http"],
        "paths": {
            "/pet/findByStatus": {
                "get": {
                    "operationId": "findPetsByStatus",
                    "parameters": [],
                    "responses": {"200": {"description": "successful"}},
                }
            }
        },
    }


def test_store_supports_petstore_url_and_conditional_content_cache(tmp_path: Path) -> None:
    document = _petstore_document()
    api = FakeApi(
        [
            _bounded(200, document, etag='"schema-v1"'),
            _bounded(304, None),
        ]
    )
    settings = SimpleNamespace(
        generated_dir=tmp_path / "generated",
        openapi_max_bytes=2_000_000,
    )
    store = OpenApiStore(cast("Any", settings), cast("Any", api))
    source = "https://petstore.swagger.io/v2/swagger.json"

    first = asyncio.run(store.load_url(source))
    second = asyncio.run(store.load_url(source))

    assert len(first.schema_id) == SCHEMA_ID_LENGTH
    assert first.schema_id == second.schema_id
    assert first.schema_version == "2.0"
    assert first.base_url == "https://petstore.swagger.io/v2"
    assert first.source_url == source
    assert first.cache_file.is_file()
    assert api.calls[1]["headers"] == {"If-None-Match": '"schema-v1"'}
    assert first.operations()[0].operation_id == "findPetsByStatus"
    if os.name != "nt":
        cache_root = settings.generated_dir / "openapi"
        assert stat.S_IMODE(cache_root.stat().st_mode) == PRIVATE_DIRECTORY_MODE
        assert stat.S_IMODE(first.cache_file.stat().st_mode) == PRIVATE_FILE_MODE
        assert stat.S_IMODE((cache_root / "index.json").stat().st_mode) == PRIVATE_FILE_MODE
        assert stat.S_IMODE((cache_root / ".index.lock").stat().st_mode) == PRIVATE_FILE_MODE


def test_protected_schema_headers_are_not_cached_or_conditionally_reused(
    tmp_path: Path,
) -> None:
    document = _petstore_document()
    api = FakeApi(
        [
            _bounded(200, document, etag='"schema-v1"'),
            _bounded(200, document, etag='"schema-v1"'),
        ]
    )
    settings = SimpleNamespace(
        generated_dir=tmp_path / "generated",
        openapi_max_bytes=2_000_000,
    )
    store = OpenApiStore(cast("Any", settings), cast("Any", api))
    authorization_value = "Bearer private-schema-credential"
    headers = {"Authorization": authorization_value}

    first = asyncio.run(store.load_url("https://schema.example.test/openapi.json", headers=headers))
    second = asyncio.run(
        store.load_url("https://schema.example.test/openapi.json", headers=headers)
    )

    assert first.schema_id == second.schema_id
    assert api.calls[0]["headers"] == headers
    assert api.calls[1]["headers"] == headers
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in settings.generated_dir.rglob("*")
        if path.is_file() and path.suffix != ".lock"
    )
    assert authorization_value not in persisted
    assert hashlib.sha256(authorization_value.encode()).hexdigest() not in persisted
    index = json.loads(store._index_path.read_text(encoding="utf-8"))
    assert index["schemaVersion"] == "2.0"
    assert index["requests"] == {}
    assert [entry["schemaId"] for entry in index["sources"].values()] == [first.schema_id]


def test_external_schema_references_are_blocked_before_validation() -> None:
    document = _petstore_document()
    document["definitions"] = {"Unsafe": {"$ref": "https://metadata.example/internal.json"}}

    with pytest.raises(ApiActivityError, match="External OpenAPI references are blocked"):
        _validate_document(document)


def test_malformed_cache_index_entry_is_rejected(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        generated_dir=tmp_path / "generated",
        openapi_max_bytes=2_000_000,
    )
    store = OpenApiStore(cast("Any", settings), cast("Any", FakeApi([])))
    store._root.mkdir(parents=True)
    store._index_path.write_text(
        json.dumps(
            {
                "schemaVersion": "2.0",
                "requests": {},
                "sources": {"a" * 64: []},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ApiActivityError, match="source entries are invalid"):
        store._index_entry("source")
