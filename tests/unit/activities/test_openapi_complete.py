"""Complete offline coverage for OpenAPI loading and operation resolution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.api import openapi
from plantain.activities.api.client import ApiActivityError, BoundedResponse
from plantain.activities.api.openapi import (
    LoadedOpenApi,
    OpenApiStore,
    resolve_local_ref,
)
from plantain.errors import AtomicPersistenceError
from plantain.models.api import HttpMethod

HTTP_BAD_GATEWAY = 502
HTTP_NOT_MODIFIED = 304
OPENAPI_LIMIT_BYTES = 2_000_000
SCHEMA_DIGEST = "a" * 64


class FakeApi:
    """Deterministic network-free API session."""

    def __init__(self, responses: list[BoundedResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def request(self, **kwargs: Any) -> BoundedResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _response(
    status: int,
    body: Any,
    *,
    headers: dict[str, str] | None = None,
) -> BoundedResponse:
    content = b"" if body is None else json.dumps(body).encode("utf-8")
    return BoundedResponse(
        request_url="https://schema.example.test/openapi.json",
        method=HttpMethod.GET,
        status_code=status,
        headers={"content-type": "application/json", **(headers or {})},
        content=content,
        elapsed_ms=1,
    )


def _settings(tmp_path: Path) -> Any:
    return SimpleNamespace(
        generated_dir=tmp_path / "generated",
        openapi_max_bytes=OPENAPI_LIMIT_BYTES,
    )


def _swagger_document() -> dict[str, Any]:
    return {
        "swagger": "2.0",
        "info": {"title": "Example", "version": "1.0"},
        "host": "api.example.test",
        "basePath": "/v1",
        "schemes": ["https"],
        "paths": {
            "/pets": {
                "parameters": [
                    {"name": "limit", "in": "query", "type": "integer"},
                    {"$ref": "#/parameters/TraceHeader"},
                    "ignored",
                ],
                "get": {
                    "operationId": "listPets",
                    "summary": "List pets",
                    "parameters": [
                        {"name": "limit", "in": "query", "type": "string"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {
                    "responses": {"201": {"description": "created"}},
                },
                "trace": {"responses": {}},
                "delete": "ignored",
            },
            7: {},
            "/ignored": "not-an-object",
        },
        "parameters": {"TraceHeader": {"name": "X-Trace", "in": "header", "type": "string"}},
    }


def _download_document() -> dict[str, Any]:
    return {
        "swagger": "2.0",
        "info": {"title": "Example", "version": "1.0"},
        "host": "api.example.test",
        "basePath": "/v1",
        "schemes": ["https"],
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def _loaded(document: dict[str, Any]) -> LoadedOpenApi:
    return LoadedOpenApi(
        schema_id=SCHEMA_DIGEST,
        schema_version="2.0",
        source_url="https://schema.example.test/openapi.json",
        base_url="https://api.example.test/v1",
        cache_file=Path("schema.json"),
        document=document,
    )


def test_operations_merge_parameters_and_emit_sorted_summaries() -> None:
    loaded = _loaded(_swagger_document())

    operations = loaded.operations()
    summaries = loaded.summaries()

    assert [(item.path, item.method) for item in operations] == [
        ("/pets", HttpMethod.GET),
        ("/pets", HttpMethod.POST),
    ]
    assert operations[0].parameters == [
        {"name": "limit", "in": "query", "type": "string"},
        {"name": "X-Trace", "in": "header", "type": "string"},
    ]
    assert summaries[0].summary == "List pets"
    assert summaries[1].operation_id is None
    assert summaries[1].summary is None


def test_operations_handle_missing_paths_and_non_object_reference() -> None:
    assert _loaded({"swagger": "2.0"}).operations() == []
    document = {
        "swagger": "2.0",
        "paths": {"/pets": {"$ref": "#/pathItems/Invalid"}},
        "pathItems": {"Invalid": []},
    }
    assert _loaded(document).operations() == []


def test_operation_selectors_require_exactly_one_match() -> None:
    loaded = _loaded(_swagger_document())

    selected = loaded.operation(operation_id="listPets", path=None, method=None)
    assert selected.method is HttpMethod.GET
    assert (
        loaded.operation(operation_id=None, path="/pets", method=HttpMethod.POST).method
        is HttpMethod.POST
    )
    with pytest.raises(ApiActivityError, match="matched 0 operations"):
        loaded.operation(operation_id="missing", path=None, method=None)

    document = _swagger_document()
    cast("dict[str, Any]", document["paths"])["/other"] = {
        "get": {
            "operationId": "listPets",
            "responses": {"200": {"description": "ok"}},
        }
    }
    with pytest.raises(ApiActivityError, match="matched 2 operations"):
        _loaded(document).operation(operation_id="listPets", path=None, method=None)


def test_conditional_cache_supports_last_modified(tmp_path: Path) -> None:
    document = _download_document()
    api = FakeApi(
        [
            _response(200, document, headers={"last-modified": "yesterday"}),
            _response(HTTP_NOT_MODIFIED, None),
        ]
    )
    store = OpenApiStore(_settings(tmp_path), cast("Any", api))

    first = asyncio.run(store.load_url("https://schema.example.test/openapi.json"))
    second = asyncio.run(store.load_url("https://schema.example.test/openapi.json"))

    assert second.schema_id == first.schema_id
    assert api.calls[1]["headers"] == {"If-Modified-Since": "yesterday"}


@pytest.mark.parametrize("status", [HTTP_NOT_MODIFIED, HTTP_BAD_GATEWAY])
def test_schema_download_rejects_unusable_http_status(tmp_path: Path, status: int) -> None:
    store = OpenApiStore(
        _settings(tmp_path),
        cast("Any", FakeApi([_response(status, None)])),
    )

    with pytest.raises(ApiActivityError, match=f"HTTP {status}"):
        asyncio.run(store.load_url("https://schema.example.test/openapi.json"))


def test_load_id_validates_digest_file_content_and_source_metadata(tmp_path: Path) -> None:
    store = OpenApiStore(_settings(tmp_path), cast("Any", FakeApi([])))

    with pytest.raises(ApiActivityError, match="lowercase SHA-256"):
        asyncio.run(store.load_id("invalid"))
    with pytest.raises(ApiActivityError, match="does not exist"):
        asyncio.run(store.load_id(SCHEMA_DIGEST))

    store._root.mkdir(parents=True)
    target = store._root / f"{SCHEMA_DIGEST}.json"
    target.write_text("not-json", encoding="utf-8")
    with pytest.raises(ApiActivityError, match="is invalid"):
        asyncio.run(store.load_id(SCHEMA_DIGEST))

    target.write_text("[]", encoding="utf-8")
    with pytest.raises(ApiActivityError, match="root must be an object"):
        asyncio.run(store.load_id(SCHEMA_DIGEST))

    document = _download_document()
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    schema_id = hashlib.sha256(canonical).hexdigest()
    target = store._root / f"{schema_id}.json"
    target.write_bytes(canonical + b"\n")
    with pytest.raises(ApiActivityError, match="has no source metadata"):
        asyncio.run(store.load_id(schema_id))

    source_url = "https://schema.example.test/openapi.json"
    source_key = hashlib.sha256(source_url.encode()).hexdigest()
    store._index_path.write_text(
        json.dumps(
            {
                "schemaVersion": "2.0",
                "requests": {},
                "sources": {
                    source_key: {
                        "schemaId": schema_id,
                        "sourceUrl": source_url,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = asyncio.run(store.load_id(schema_id))
    assert loaded.source_url == source_url


@pytest.mark.parametrize(
    "content, message",
    [
        ("not-json", "cache index is invalid"),
        ("[]", "must be an object"),
        (json.dumps({"schemaVersion": "1.0"}), "unsupported schema version"),
        (
            json.dumps({"schemaVersion": "2.0"}),
            "must contain request and source objects",
        ),
    ],
)
def test_cache_index_rejects_corrupt_shapes(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    store = OpenApiStore(_settings(tmp_path), cast("Any", FakeApi([])))
    store._root.mkdir(parents=True)
    store._index_path.write_text(content, encoding="utf-8")

    with pytest.raises(ApiActivityError, match=message):
        store._read_index()


def test_empty_cache_index_and_unknown_source_are_safe(tmp_path: Path) -> None:
    store = OpenApiStore(_settings(tmp_path), cast("Any", FakeApi([])))
    store._root.mkdir(parents=True)

    assert store._read_index() == {
        "schemaVersion": "2.0",
        "requests": {},
        "sources": {},
    }
    assert store._index_entry("unknown") is None


def test_local_references_support_pointer_escaping_and_chains() -> None:
    document = {
        "components": {
            "schemas": {
                "Pet/Record": {"~type": {"$ref": "#/definitions/Name"}},
            }
        },
        "definitions": {"Name": {"type": "string"}},
    }

    assert resolve_local_ref(document, {"type": "integer"}) == {"type": "integer"}
    assert resolve_local_ref(
        document,
        {"$ref": "#/components/schemas/Pet~1Record/~0type"},
    ) == {"type": "string"}


@pytest.mark.parametrize("reference", [7, "https://schema.example.test/model.json"])
def test_local_references_reject_unsupported_reference(reference: Any) -> None:
    with pytest.raises(ApiActivityError, match="Only local"):
        resolve_local_ref({}, {"$ref": reference})


def test_local_references_reject_missing_and_excessive_depth() -> None:
    with pytest.raises(ApiActivityError, match="does not exist"):
        resolve_local_ref({}, {"$ref": "#/missing"})
    with pytest.raises(ApiActivityError, match="nesting exceeds"):
        resolve_local_ref({}, {"$ref": "#/missing"}, depth=openapi.MAX_REFERENCE_DEPTH)


def test_parse_document_accepts_objects_and_yaml_and_rejects_unsafe_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _swagger_document()
    assert openapi._parse_document(document) is document
    assert openapi._parse_document("swagger: '2.0'\npaths: {}") == {
        "swagger": "2.0",
        "paths": {},
    }

    with pytest.raises(ApiActivityError, match="must be JSON or UTF-8 YAML"):
        openapi._parse_document([])
    with pytest.raises(ApiActivityError, match="not valid JSON or YAML"):
        openapi._parse_document("value: [")
    with pytest.raises(ApiActivityError, match="root must be an object"):
        openapi._parse_document("- item")

    monkeypatch.setattr(openapi, "MAX_OPENAPI_TEXT_BYTES", 3)
    with pytest.raises(ApiActivityError, match="hard 20 MiB parser limit"):
        openapi._parse_document("four")


def test_validate_document_enforces_structure_and_wraps_validator_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openapi, "validate", lambda _document: None)
    openapi._validate_document({"openapi": "3.1.0", "items": [1, {"type": "string"}]})

    monkeypatch.setattr(openapi, "MAX_OPENAPI_NODES", 1)
    with pytest.raises(ApiActivityError, match="structural node limit"):
        openapi._validate_document({"openapi": "3.1.0", "child": {}})

    monkeypatch.setattr(openapi, "MAX_OPENAPI_NODES", openapi.MAX_OPENAPI_NODES + 10)

    def reject(_document: Any) -> None:
        raise ValueError("untrusted validator detail")

    monkeypatch.setattr(openapi, "validate", reject)
    with pytest.raises(ApiActivityError, match="ValueError") as captured:
        openapi._validate_document({"openapi": "3.1.0"})
    assert "untrusted validator detail" not in str(captured.value)


@pytest.mark.parametrize(
    "document, expected",
    [
        ({"swagger": "2.0"}, "2.0"),
        ({"openapi": "3.1.0"}, "3.1.0"),
    ],
)
def test_schema_version_accepts_supported_documents(
    document: dict[str, Any],
    expected: str,
) -> None:
    assert openapi._schema_version(document) == expected


def test_schema_version_rejects_unknown_document() -> None:
    with pytest.raises(ApiActivityError, match=r"supported Swagger 2 or OpenAPI 3.0/3.1"):
        openapi._schema_version({"openapi": "2.0"})


def test_base_url_resolves_swagger_defaults_and_rejects_missing_host() -> None:
    assert (
        openapi._base_url(
            {"swagger": "2.0", "basePath": "/v2/"},
            "http://api.example.test/schema.json",
        )
        == "http://api.example.test/v2"
    )
    assert (
        openapi._base_url(
            {"swagger": "2.0", "host": "api.example.test", "schemes": []},
            "schema.json",
        )
        == "https://api.example.test"
    )
    with pytest.raises(ApiActivityError, match="resolvable host"):
        openapi._base_url({"swagger": "2.0"}, "schema.json")


def test_base_url_resolves_openapi_servers_variables_and_fallbacks() -> None:
    document = {
        "openapi": "3.0.3",
        "servers": [
            {
                "url": "/{version}",
                "variables": {"version": {"default": "v3"}},
            }
        ],
    }
    assert (
        openapi._base_url(document, "https://schema.example.test/openapi.json")
        == "https://schema.example.test/v3"
    )
    assert (
        openapi._base_url(
            {"openapi": "3.0.3", "servers": "invalid"},
            "https://schema.example.test/openapi.json",
        )
        == "https://schema.example.test"
    )
    with pytest.raises(ApiActivityError, match="has no default"):
        openapi._base_url(
            {"openapi": "3.0.3", "servers": [{"url": "{region}", "variables": []}]},
            "https://schema.example.test/openapi.json",
        )
    with pytest.raises(ApiActivityError, match="resolvable server"):
        openapi._base_url({"openapi": "3.0.3"}, "openapi.json")


def test_atomic_cache_write_translates_filesystem_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(_path: Path, _value: bytes) -> None:
        raise AtomicPersistenceError("synthetic persistence failure")

    monkeypatch.setattr(openapi, "write_bytes_atomic", reject)
    with pytest.raises(ApiActivityError, match="cache persistence failed"):
        openapi._write_bytes_atomic(tmp_path / "schema.json", b"{}")
