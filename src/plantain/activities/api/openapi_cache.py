"""Bounded content-addressed persistence for validated OpenAPI documents."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from plantain.activities.api.client import ApiActivityError
from plantain.errors import AtomicPersistenceError
from plantain.persistence import (
    ensure_private_directory,
    open_binary_read_no_follow,
    private_file_lock,
    write_bytes_atomic,
)

JsonObject = dict[str, Any]
CACHE_INDEX_VERSION = "2.0"
MAX_CACHE_INDEX_BYTES = 16_777_216
MAX_CACHE_ENTRIES = 10_000
MAX_CACHED_DOCUMENTS = 10_000
MAX_CACHE_VALIDATOR_LENGTH = 4_096
MAX_SOURCE_URL_LENGTH = 8_192
HTTP_DEFAULT_PORT = 80
HTTPS_DEFAULT_PORT = 443

_DIGEST = re.compile(r"[a-f0-9]{64}")
_CACHE_DOCUMENT = re.compile(r"[a-f0-9]{64}\.json")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class CacheBinding:
    """Safe source and conditional-request metadata for one downloaded document."""

    source_url: str
    request_identity: str | None
    etag: str | None = None
    last_modified: str | None = None


class OpenApiCache:
    """Private bounded cache with independently verified source bindings."""

    def __init__(self, root: Path, *, max_document_bytes: int) -> None:
        if max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")
        self.root = root
        self.index_path = root / "index.json"
        self.lock_path = root / ".index.lock"
        self.max_document_bytes = max_document_bytes

    def ensure(self) -> None:
        try:
            ensure_private_directory(self.root)
        except AtomicPersistenceError as exc:
            raise ApiActivityError("OpenAPI cache persistence failed") from exc

    def request_entry(self, identity: str) -> JsonObject | None:
        with private_file_lock(self.lock_path, timeout=30):
            entry = self.read_index()["requests"].get(identity)
        return dict(entry) if isinstance(entry, dict) else None

    def source_for_identity(self, identity: str, schema_id: str) -> str:
        with private_file_lock(self.lock_path, timeout=30):
            entry = self.read_index()["sources"].get(identity)
        if not isinstance(entry, dict) or entry.get("schemaId") != schema_id:
            raise ApiActivityError("Cached OpenAPI source binding is unavailable")
        return str(entry["sourceUrl"])

    def source_for_schema(self, schema_id: str) -> str:
        with private_file_lock(self.lock_path, timeout=30):
            sources = self.read_index()["sources"]
        matches = {
            str(entry["sourceUrl"])
            for entry in sources.values()
            if isinstance(entry, dict) and entry.get("schemaId") == schema_id
        }
        if not matches:
            raise ApiActivityError(
                f"Cached OpenAPI schema '{schema_id}' has no source metadata; load it by URL again"
            )
        if len(matches) != 1:
            raise ApiActivityError(
                f"Cached OpenAPI schema '{schema_id}' has ambiguous source metadata; load it by URL"
            )
        return matches.pop()

    def load_document(self, schema_id: str) -> tuple[JsonObject, Path]:
        _validated_digest(schema_id, "OpenAPI schemaId must be a lowercase SHA-256 digest")
        target = self.root / f"{schema_id}.json"
        if target.parent != self.root or not target.is_file():
            raise ApiActivityError(f"Cached OpenAPI schema '{schema_id}' does not exist")
        raw = self._read_document(target, schema_id)
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiActivityError(f"Cached OpenAPI schema '{schema_id}' is invalid") from exc
        if not isinstance(document, dict):
            raise ApiActivityError("Cached OpenAPI root must be an object")
        canonical = canonical_document(document, max_bytes=self.max_document_bytes)
        if hashlib.sha256(canonical).hexdigest() != schema_id:
            raise ApiActivityError(
                f"Cached OpenAPI schema '{schema_id}' does not match its content digest"
            )
        return document, target

    def persist(
        self,
        schema_id: str,
        canonical: bytes,
        binding: CacheBinding,
    ) -> Path:
        self.ensure()
        source_url = canonical_source_url(binding.source_url)
        source_key = source_identity(source_url)
        target = self.root / f"{schema_id}.json"
        try:
            with private_file_lock(self.lock_path, timeout=30):
                index = self.read_index()
                requests = index["requests"]
                sources = index["sources"]
                _assert_entry_capacity(sources, source_key, "source")
                if binding.request_identity is not None:
                    _assert_entry_capacity(requests, binding.request_identity, "request")
                self._assert_document_capacity(target)
                sources[source_key] = {
                    "schemaId": schema_id,
                    "sourceUrl": source_url,
                }
                if binding.request_identity is not None:
                    requests[binding.request_identity] = _request_entry(
                        schema_id,
                        source_key,
                        binding,
                    )
                encoded_index = _encoded_index(index)
                write_bytes_atomic(target, canonical + b"\n")
                write_bytes_atomic(self.index_path, encoded_index)
        except (OSError, AtomicPersistenceError) as exc:
            raise ApiActivityError("OpenAPI cache persistence failed") from exc
        return target

    def _assert_document_capacity(self, target: Path) -> None:
        if not target.exists() and self._document_count() >= MAX_CACHED_DOCUMENTS:
            raise ApiActivityError("OpenAPI cache document capacity was exceeded")

    def read_index(self) -> JsonObject:
        try:
            with open_binary_read_no_follow(self.index_path, private=True) as handle:
                raw = handle.read(MAX_CACHE_INDEX_BYTES + 1)
        except FileNotFoundError:
            return _empty_index()
        except (OSError, AtomicPersistenceError) as exc:
            raise ApiActivityError("OpenAPI cache index is invalid") from exc
        if len(raw) > MAX_CACHE_INDEX_BYTES:
            raise ApiActivityError("OpenAPI cache index exceeds its byte limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiActivityError("OpenAPI cache index is invalid") from exc
        return _validated_index(value)

    def _read_document(self, target: Path, schema_id: str) -> bytes:
        try:
            with open_binary_read_no_follow(target, private=True) as handle:
                raw = handle.read(self.max_document_bytes + 2)
        except (FileNotFoundError, OSError, AtomicPersistenceError) as exc:
            raise ApiActivityError(f"Cached OpenAPI schema '{schema_id}' is invalid") from exc
        payload = raw[:-1] if raw.endswith(b"\n") else raw
        if len(payload) > self.max_document_bytes:
            raise ApiActivityError(f"Cached OpenAPI schema '{schema_id}' exceeds its byte limit")
        return payload

    def _document_count(self) -> int:
        return sum(
            1 for item in self.root.iterdir() if _CACHE_DOCUMENT.fullmatch(item.name) is not None
        )


def canonical_source_url(value: str) -> str:
    """Return the query-free absolute identity used for relative server resolution."""

    if len(value) > MAX_SOURCE_URL_LENGTH:
        raise ApiActivityError("OpenAPI source metadata exceeds its length limit")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ApiActivityError("OpenAPI source metadata is invalid") from exc
    scheme = parts.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ApiActivityError("OpenAPI source metadata is invalid")
    hostname = parts.hostname.casefold()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (scheme == "http" and port == HTTP_DEFAULT_PORT) or (
        scheme == "https" and port == HTTPS_DEFAULT_PORT
    )
    netloc = rendered_host if port is None or default_port else f"{rendered_host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", "", ""))


def source_identity(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def request_identity(url: str, *, has_headers: bool) -> str | None:
    parts = urlsplit(url)
    if has_headers or parts.query or parts.fragment:
        return None
    return source_identity(canonical_source_url(url))


def canonical_document(document: JsonObject, *, max_bytes: int) -> bytes:
    """Encode deterministic JSON without retaining data beyond the configured envelope."""

    encoded = bytearray()
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    try:
        for fragment in encoder.iterencode(document):
            _append_canonical_fragment(encoded, fragment, max_bytes)
    except ApiActivityError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise ApiActivityError("OpenAPI document cannot be canonicalized safely") from exc
    return bytes(encoded)


def _append_canonical_fragment(
    encoded: bytearray,
    fragment: str,
    max_bytes: int,
) -> None:
    encoded.extend(fragment.encode("utf-8"))
    if len(encoded) > max_bytes:
        raise ApiActivityError("OpenAPI canonical document exceeds its byte limit")


def cache_validator(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_CACHE_VALIDATOR_LENGTH
        or _CONTROL_CHARACTER.search(value) is not None
    ):
        return None
    return value


def _empty_index() -> JsonObject:
    return {
        "schemaVersion": CACHE_INDEX_VERSION,
        "requests": {},
        "sources": {},
    }


def _validated_index(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise ApiActivityError("OpenAPI cache index must be an object")
    if value.get("schemaVersion") != CACHE_INDEX_VERSION:
        raise ApiActivityError("OpenAPI cache index uses an unsupported schema version")
    requests = value.get("requests")
    sources = value.get("sources")
    if not isinstance(requests, dict) or not isinstance(sources, dict):
        raise ApiActivityError("OpenAPI cache index must contain request and source objects")
    if len(requests) > MAX_CACHE_ENTRIES or len(sources) > MAX_CACHE_ENTRIES:
        raise ApiActivityError("OpenAPI cache index exceeds its entry limit")
    for identity, entry in sources.items():
        _validate_source_entry(identity, entry)
    for identity, entry in requests.items():
        _validate_request_entry(identity, entry, sources)
    return value


def _validate_source_entry(identity: Any, entry: Any) -> None:
    _validated_digest(identity, "OpenAPI cache source identity is invalid")
    if not isinstance(entry, dict) or set(entry) != {"schemaId", "sourceUrl"}:
        raise ApiActivityError("OpenAPI cache source entries are invalid")
    _validated_digest(entry.get("schemaId"), "OpenAPI cache schema identity is invalid")
    source_url = entry.get("sourceUrl")
    if not isinstance(source_url, str):
        raise ApiActivityError("OpenAPI cache source URL is invalid")
    canonical = canonical_source_url(source_url)
    if canonical != source_url or source_identity(canonical) != identity:
        raise ApiActivityError("OpenAPI cache source binding is invalid")


def _validate_request_entry(identity: Any, entry: Any, sources: JsonObject) -> None:
    _validated_digest(identity, "OpenAPI cache request identity is invalid")
    if not isinstance(entry, dict):
        raise ApiActivityError("OpenAPI cache request entries are invalid")
    required = {"schemaId", "sourceIdentity"}
    if not required.issubset(entry) or not set(entry).issubset({*required, "etag", "lastModified"}):
        raise ApiActivityError("OpenAPI cache request entries are invalid")
    schema_id = _validated_digest(
        entry.get("schemaId"),
        "OpenAPI cache request schema identity is invalid",
    )
    source_key = _validated_digest(
        entry.get("sourceIdentity"),
        "OpenAPI cache request source identity is invalid",
    )
    source = sources.get(source_key)
    if not isinstance(source, dict) or source.get("schemaId") != schema_id:
        raise ApiActivityError("OpenAPI cache request binding is invalid")
    for name in ("etag", "lastModified"):
        if name in entry and cache_validator(entry[name]) is None:
            raise ApiActivityError("OpenAPI cache validator metadata is invalid")


def _validated_digest(value: Any, message: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ApiActivityError(message)
    return value


def _assert_entry_capacity(entries: JsonObject, key: str, label: str) -> None:
    if key not in entries and len(entries) >= MAX_CACHE_ENTRIES:
        raise ApiActivityError(f"OpenAPI cache {label} capacity was exceeded")


def _request_entry(
    schema_id: str,
    source_key: str,
    binding: CacheBinding,
) -> JsonObject:
    entry: JsonObject = {
        "schemaId": schema_id,
        "sourceIdentity": source_key,
    }
    if binding.etag is not None:
        entry["etag"] = binding.etag
    if binding.last_modified is not None:
        entry["lastModified"] = binding.last_modified
    return entry


def _encoded_index(index: JsonObject) -> bytes:
    _validated_index(index)
    encoded = (
        json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_CACHE_INDEX_BYTES:
        raise ApiActivityError("OpenAPI cache index exceeds its byte limit")
    return encoded


__all__ = [
    "CACHE_INDEX_VERSION",
    "CacheBinding",
    "OpenApiCache",
    "cache_validator",
    "canonical_document",
    "canonical_source_url",
    "request_identity",
    "source_identity",
]
