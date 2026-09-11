"""On-demand Swagger 2/OpenAPI 3 loading, caching, and operation resolution."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import yaml
from openapi_spec_validator import validate

from plantain.activities.api.client import ApiActivityError, ApiSession, BoundedResponse
from plantain.activities.api.openapi_cache import (
    CacheBinding,
    OpenApiCache,
    cache_validator,
    canonical_document,
    canonical_source_url,
    request_identity,
)
from plantain.activities.api.validation_worker import run_isolated_api_validation
from plantain.config import Settings
from plantain.engine.admission import ResourceAdmission
from plantain.errors import AtomicPersistenceError
from plantain.models.api import ApiOperationSummary, HttpMethod
from plantain.persistence import write_bytes_atomic

JsonObject = dict[str, Any]
_HTTP_METHODS = {item.value.casefold() for item in HttpMethod}
_SERVER_VARIABLE = re.compile(r"\{([A-Za-z0-9_.-]+)\}")
_SUPPORTED_OPENAPI_VERSION = re.compile(r"3\.(?:0|1)\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
HTTP_OK = 200
HTTP_NOT_MODIFIED = 304
MAX_REFERENCE_DEPTH = 50
MAX_OPENAPI_TEXT_BYTES = 20_971_520
MAX_OPENAPI_NODES = 500_000


@dataclass(frozen=True, slots=True)
class OpenApiOperation:
    """Resolved operation plus inherited path parameters."""

    method: HttpMethod
    path: str
    operation_id: str | None
    document: JsonObject
    path_item: JsonObject
    operation: JsonObject

    @property
    def parameters(self) -> list[JsonObject]:
        merged: dict[tuple[str, str], JsonObject] = {}
        for source in (self.path_item.get("parameters", []), self.operation.get("parameters", [])):
            if not isinstance(source, list):
                continue
            for raw in source:
                parameter = resolve_local_ref(self.document, raw)
                if not isinstance(parameter, dict):
                    continue
                key = (str(parameter.get("in") or ""), str(parameter.get("name") or ""))
                merged[key] = parameter
        return list(merged.values())


@dataclass(frozen=True, slots=True)
class LoadedOpenApi:
    """Validated immutable schema document and derived metadata."""

    schema_id: str
    schema_version: str
    source_url: str
    base_url: str
    cache_file: Path
    document: JsonObject

    def operations(self) -> list[OpenApiOperation]:
        paths = self.document.get("paths")
        if not isinstance(paths, dict):
            return []
        result: list[OpenApiOperation] = []
        for path, path_item in paths.items():
            if not isinstance(path, str) or not isinstance(path_item, dict):
                continue
            resolved_path = resolve_local_ref(self.document, path_item)
            if not isinstance(resolved_path, dict):
                continue
            for method, operation in resolved_path.items():
                if str(method).casefold() not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue
                result.append(
                    OpenApiOperation(
                        method=HttpMethod(str(method).upper()),
                        path=path,
                        operation_id=(
                            str(operation["operationId"])
                            if operation.get("operationId") is not None
                            else None
                        ),
                        document=self.document,
                        path_item=resolved_path,
                        operation=operation,
                    )
                )
        return sorted(result, key=lambda item: (item.path, item.method.value))

    def summaries(self) -> list[ApiOperationSummary]:
        return [
            ApiOperationSummary(
                operation_id=item.operation_id,
                method=item.method,
                path=item.path,
                summary=(
                    str(item.operation["summary"])
                    if item.operation.get("summary") is not None
                    else None
                ),
            )
            for item in self.operations()
        ]

    def operation(
        self,
        *,
        operation_id: str | None,
        path: str | None,
        method: HttpMethod | None,
    ) -> OpenApiOperation:
        if operation_id is not None:
            matches = [item for item in self.operations() if item.operation_id == operation_id]
            selector = f"operationId '{operation_id}'"
        else:
            matches = [
                item for item in self.operations() if item.path == path and item.method is method
            ]
            selector = f"{method} {path}"
        if len(matches) != 1:
            raise ApiActivityError(
                f"OpenAPI selector {selector} matched {len(matches)} operations; "
                "exactly one is required"
            )
        return matches[0]


class OpenApiStore:
    """Content-addressed, concurrency-safe schema store initialized only on demand."""

    def __init__(
        self,
        settings: Settings,
        api: ApiSession,
        *,
        admission: ResourceAdmission | None = None,
    ) -> None:
        self._settings = settings
        self._api = api
        self._admission = admission or ResourceAdmission(settings)
        self._cache = OpenApiCache(
            settings.generated_dir / "openapi",
            max_document_bytes=settings.openapi_max_bytes,
        )
        self._root = self._cache.root
        self._index_path = self._cache.index_path
        self._lock_path = self._cache.lock_path

    async def load_url(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> LoadedOpenApi:
        await self._admission.run_blocking(
            self._cache.ensure,
        )
        supplied_headers = dict(headers or {})
        identity = request_identity(url, has_headers=bool(supplied_headers))
        cached = (
            None
            if identity is None
            else await self._admission.run_blocking(
                self._cache.request_entry,
                identity,
            )
        )
        response = await self._request_document(
            url,
            _conditional_headers(supplied_headers, cached),
        )
        if response.status_code == HTTP_NOT_MODIFIED and cached:
            try:
                return await self._load_request_entry(cached)
            except ApiActivityError:
                response = await self._request_document(url, supplied_headers)
        return await self._persist_response(response, identity)

    async def _request_document(
        self,
        url: str,
        headers: dict[str, str],
    ) -> BoundedResponse:
        return await self._api.request(
            method=HttpMethod.GET,
            url=url,
            headers=headers,
            max_response_bytes=self._settings.openapi_max_bytes,
        )

    async def _load_request_entry(self, entry: JsonObject) -> LoadedOpenApi:
        schema_id = entry.get("schemaId")
        source_key = entry.get("sourceIdentity")
        if not isinstance(schema_id, str) or not isinstance(source_key, str):
            raise ApiActivityError("OpenAPI cache request binding is invalid")
        return await self._load_cached(schema_id, source_key=source_key)

    async def _persist_response(
        self,
        response: BoundedResponse,
        identity: str | None,
    ) -> LoadedOpenApi:
        if response.status_code != HTTP_OK:
            raise ApiActivityError(
                f"OpenAPI download returned HTTP {response.status_code}; expected 200"
            )
        document = _parse_document(response.decoded_body())
        await run_isolated_api_validation(
            self._admission,
            self._settings,
            _validate_document,
            document,
        )
        canonical = canonical_document(
            document,
            max_bytes=self._settings.openapi_max_bytes,
        )
        schema_id = hashlib.sha256(canonical).hexdigest()
        source_url = canonical_source_url(response.request_url)
        binding = CacheBinding(
            source_url=source_url,
            request_identity=identity,
            etag=cache_validator(response.headers.get("etag")),
            last_modified=cache_validator(response.headers.get("last-modified")),
        )
        target = await self._admission.run_blocking(
            self._cache.persist,
            schema_id,
            canonical,
            binding,
        )
        return self._loaded(schema_id, document, target, source_url)

    async def load_id(self, schema_id: str) -> LoadedOpenApi:
        return await self._load_cached(schema_id)

    async def _load_cached(
        self,
        schema_id: str,
        *,
        source_key: str | None = None,
    ) -> LoadedOpenApi:
        document, target = await self._admission.run_blocking(
            self._cache.load_document,
            schema_id,
        )
        await run_isolated_api_validation(
            self._admission,
            self._settings,
            _validate_document,
            document,
        )
        if source_key is None:
            source_url = await self._admission.run_blocking(
                self._cache.source_for_schema,
                schema_id,
            )
        else:
            source_url = await self._admission.run_blocking(
                self._cache.source_for_identity,
                source_key,
                schema_id,
            )
        return self._loaded(schema_id, document, target, source_url)

    def _loaded(
        self,
        schema_id: str,
        document: JsonObject,
        target: Path,
        source_url: str,
    ) -> LoadedOpenApi:
        version = _schema_version(document)
        return LoadedOpenApi(
            schema_id=schema_id,
            schema_version=version,
            source_url=source_url,
            base_url=_base_url(document, source_url),
            cache_file=target,
            document=document,
        )

    def _index_entry(self, url_key: str) -> JsonObject | None:
        return self._cache.request_entry(url_key)

    def _source_for_schema(self, schema_id: str) -> str:
        return self._cache.source_for_schema(schema_id)

    def _read_index(self) -> JsonObject:
        return self._cache.read_index()


def _conditional_headers(
    supplied: Mapping[str, str],
    cached: JsonObject | None,
) -> dict[str, str]:
    headers = dict(supplied)
    if cached is None:
        return headers
    etag = cache_validator(cached.get("etag"))
    last_modified = cache_validator(cached.get("lastModified"))
    if etag is not None:
        headers["If-None-Match"] = etag
    if last_modified is not None:
        headers["If-Modified-Since"] = last_modified
    return headers


def resolve_local_ref(document: JsonObject, value: Any, *, depth: int = 0) -> Any:
    """Resolve a local JSON Pointer with cycle and depth protection."""

    if not isinstance(value, dict) or "$ref" not in value:
        return value
    reference = value.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ApiActivityError("Only local OpenAPI $ref values are supported")
    if depth >= MAX_REFERENCE_DEPTH:
        raise ApiActivityError("OpenAPI reference nesting exceeds the limit of 50")
    current: Any = document
    for raw_token in reference[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise ApiActivityError(f"OpenAPI reference does not exist: {reference}")
        current = current[token]
    if isinstance(current, dict) and "$ref" in current:
        return resolve_local_ref(document, current, depth=depth + 1)
    return current


def _parse_document(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ApiActivityError("OpenAPI response must be JSON or UTF-8 YAML")
    if len(value.encode("utf-8")) > MAX_OPENAPI_TEXT_BYTES:
        raise ApiActivityError("OpenAPI text exceeds the hard 20 MiB parser limit")
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise ApiActivityError("OpenAPI response is not valid JSON or YAML") from exc
    if not isinstance(parsed, dict):
        raise ApiActivityError("OpenAPI document root must be an object")
    return parsed


def _validate_document(document: JsonObject) -> None:
    _schema_version(document)
    node_count = 0
    stack: list[Any] = [document]
    while stack:
        current = stack.pop()
        node_count += 1
        if node_count > MAX_OPENAPI_NODES:
            raise ApiActivityError("OpenAPI document exceeds the structural node limit")
        if isinstance(current, dict):
            reference = current.get("$ref")
            if reference is not None and (
                not isinstance(reference, str) or not reference.startswith("#/")
            ):
                raise ApiActivityError("External OpenAPI references are blocked by default")
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    try:
        validate(document)
    except Exception as exc:
        raise ApiActivityError(
            f"OpenAPI document failed specification validation: {type(exc).__name__}"
        ) from exc


def _schema_version(document: JsonObject) -> str:
    if document.get("swagger") == "2.0":
        return "2.0"
    value = document.get("openapi")
    if isinstance(value, str) and _SUPPORTED_OPENAPI_VERSION.fullmatch(value):
        return value
    raise ApiActivityError("Document is not a supported Swagger 2 or OpenAPI 3.0/3.1 contract")


def _base_url(document: JsonObject, source_url: str) -> str:
    if document.get("swagger") == "2.0":
        source = urlsplit(source_url)
        schemes = document.get("schemes")
        if isinstance(schemes, list) and schemes:
            scheme = str(schemes[0])
        else:
            scheme = source.scheme or "https"
        host = str(document.get("host") or source.netloc)
        base_path = str(document.get("basePath") or "")
        if not host:
            raise ApiActivityError("Swagger 2 document does not define a resolvable host")
        return f"{scheme}://{host}{'/' + base_path.strip('/') if base_path.strip('/') else ''}"

    servers = document.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        server = servers[0]
        raw_url = str(server.get("url") or "")
        raw_variables = server.get("variables")
        variables: dict[str, Any] = raw_variables if isinstance(raw_variables, dict) else {}

        def replace(match: re.Match[str]) -> str:
            item = variables.get(match.group(1))
            if not isinstance(item, dict) or "default" not in item:
                raise ApiActivityError(f"OpenAPI server variable '{match.group(1)}' has no default")
            return str(item["default"])

        resolved = _SERVER_VARIABLE.sub(replace, raw_url)
        return urljoin(source_url, resolved)
    source = urlsplit(source_url)
    if not source.scheme or not source.netloc:
        raise ApiActivityError("OpenAPI 3 document does not define a resolvable server")
    return f"{source.scheme}://{source.netloc}"


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    try:
        write_bytes_atomic(path, value)
    except AtomicPersistenceError as exc:
        raise ApiActivityError("OpenAPI cache persistence failed") from exc
