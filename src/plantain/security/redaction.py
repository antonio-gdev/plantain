"""Secret-safe redaction for bounded logs and complete persisted artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "***REDACTED***"
MAX_ARTIFACT_REDACTION_DEPTH = 256
MAX_LOG_REDACTION_DEPTH = 20
MAX_LOG_REDACTED_ITEMS = 200

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_\-.])(?:"
    r"api[_-]?key|authorization|client[_-]?secret|cookie|credential|"
    r"password|passwd|private[_-]?key|refresh[_-]?token|secret|session|"
    r"ssn|token|x[_-]?api[_-]?key"
    r")(?:$|[_\-.])",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"eyJ[A-Za-z0-9_-]{13,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"(?![A-Za-z0-9_-])"
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)"
    r"\s*[:=]\s*([^\s,;&]+)"
)
_UNHANDLED = object()


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Immutable built-in and user-configured sensitive field-name policy."""

    sensitive_keys: tuple[str, ...] = field(default_factory=tuple, repr=False)
    _sensitive_key_set: frozenset[str] = field(
        init=False,
        default_factory=frozenset,
        repr=False,
        compare=False,
    )
    _sensitive_text_pattern: re.Pattern[str] | None = field(
        init=False, default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        normalized = tuple(
            sorted(
                {item.strip().casefold() for item in self.sensitive_keys if item.strip()},
                key=lambda item: (-len(item), item),
            )
        )
        object.__setattr__(self, "sensitive_keys", normalized)
        object.__setattr__(self, "_sensitive_key_set", frozenset(normalized))
        if normalized:
            alternatives = "|".join(re.escape(item) for item in normalized)
            object.__setattr__(
                self,
                "_sensitive_text_pattern",
                re.compile(alternatives, re.IGNORECASE),
            )

    def is_sensitive_key(self, key: object) -> bool:
        """Return whether a structured field name must have its value masked."""

        rendered = str(key)
        folded = rendered.casefold()
        return bool(_SENSITIVE_KEY.search(rendered)) or folded in self._sensitive_key_set

    def contains_sensitive_data(self, value: str) -> bool:
        """Match configured key fragments anywhere in a string."""

        return bool(
            self._sensitive_text_pattern is not None and self._sensitive_text_pattern.search(value)
        )

    def is_sensitive_reference(self, value: object) -> bool:
        """Detect a sensitive field name embedded in a locator or diagnostic hint."""

        rendered = str(value)
        normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", rendered)
        return self.is_sensitive_key(normalized) or self.contains_sensitive_data(rendered)

    def redact_text(self, value: str) -> str:
        """Redact credentials embedded in otherwise unstructured text."""

        if self.contains_sensitive_data(value):
            return REDACTED
        sanitized = _JWT.sub(REDACTED, value)
        sanitized = _BEARER.sub(
            lambda match: match.group(0).split(maxsplit=1)[0] + " " + REDACTED,
            sanitized,
        )
        return _ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}={REDACTED}",
            sanitized,
        )

    def redact_url(self, value: str) -> str:
        """Sanitize a URL while retaining its full diagnostic routing identity."""

        if self.contains_sensitive_data(value):
            return REDACTED
        try:
            parts = urlsplit(value)
        except ValueError:
            return self.redact_text(value)
        if not parts.scheme or not parts.netloc:
            return self.redact_text(value)

        hostname = parts.hostname or ""
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = f":{parts.port}" if parts.port is not None else ""
        except ValueError:
            return "<malformed-url>"
        netloc = f"{rendered_host}{port}"
        query = self._redact_query(parts.query)
        if "?" in parts.fragment:
            route, separator, fragment_query = parts.fragment.partition("?")
            fragment = f"{self.redact_text(route)}{separator}{self._redact_query(fragment_query)}"
        elif "=" in parts.fragment:
            fragment = self._redact_query(parts.fragment)
        else:
            fragment = self.redact_text(parts.fragment)
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))

    def _redact_query(self, value: str) -> str:
        try:
            query_items = parse_qsl(value, keep_blank_values=True)
        except ValueError:
            return self.redact_text(value)
        return urlencode(
            [
                (
                    key,
                    REDACTED if self.is_sensitive_key(key) else self.redact_text(item),
                )
                for key, item in query_items
            ],
            doseq=True,
        )

    def redact_log(self, value: Any) -> Any:
        """Return a size-bounded recursively redacted representation for logs."""

        return self._redact(
            value,
            depth=0,
            max_depth=MAX_LOG_REDACTION_DEPTH,
            max_items=MAX_LOG_REDACTED_ITEMS,
            ancestors=set(),
            fail_on_depth=False,
        )

    def redact_artifact(self, value: Any) -> Any:
        """Redact every reachable item without silently truncating persisted data."""

        return self._redact(
            value,
            depth=0,
            max_depth=MAX_ARTIFACT_REDACTION_DEPTH,
            max_items=None,
            ancestors=set(),
            fail_on_depth=True,
        )

    def _redact(
        self,
        value: Any,
        *,
        depth: int,
        max_depth: int | None,
        max_items: int | None,
        ancestors: set[int],
        fail_on_depth: bool,
    ) -> Any:
        if max_depth is not None and depth > max_depth:
            if fail_on_depth:
                raise RuntimeError("Artifact redaction exceeds its internal depth limit")
            return "<maximum-log-redaction-depth>"
        scalar = self._redact_scalar(value)
        if scalar is not _UNHANDLED:
            return scalar
        structured = self._structured_value(value)
        if structured is not _UNHANDLED:
            value = structured
        if isinstance(value, Mapping):
            return self._redact_mapping(
                value,
                depth=depth,
                max_depth=max_depth,
                max_items=max_items,
                ancestors=ancestors,
                fail_on_depth=fail_on_depth,
            )
        if isinstance(value, Sequence):
            return self._redact_sequence(
                value,
                depth=depth,
                max_depth=max_depth,
                max_items=max_items,
                ancestors=ancestors,
                fail_on_depth=fail_on_depth,
            )
        return self.redact_text(str(value))

    def _redact_scalar(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self.redact_url(value) if "://" in value else self.redact_text(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<bytes:{len(value)}>"
        return _UNHANDLED

    @staticmethod
    def _structured_value(value: Any) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            return {item.name: getattr(value, item.name) for item in fields(value)}
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="python")
        return _UNHANDLED

    def _redact_mapping(
        self,
        value: Mapping[Any, Any],
        *,
        depth: int,
        max_depth: int | None,
        max_items: int | None,
        ancestors: set[int],
        fail_on_depth: bool,
    ) -> dict[str, Any] | str:
        identity = id(value)
        if identity in ancestors:
            return "<cyclic-reference>"
        ancestors.add(identity)
        try:
            result: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if max_items is not None and index >= max_items:
                    result["<truncated>"] = f"{len(value) - max_items} additional entries"
                    break
                key_text = str(key)
                result[key_text] = (
                    REDACTED
                    if self.is_sensitive_key(key_text)
                    else self._redact(
                        item,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_items=max_items,
                        ancestors=ancestors,
                        fail_on_depth=fail_on_depth,
                    )
                )
            return result
        finally:
            ancestors.remove(identity)

    def _redact_sequence(
        self,
        value: Sequence[Any],
        *,
        depth: int,
        max_depth: int | None,
        max_items: int | None,
        ancestors: set[int],
        fail_on_depth: bool,
    ) -> list[Any] | str:
        identity = id(value)
        if identity in ancestors:
            return "<cyclic-reference>"
        ancestors.add(identity)
        try:
            rendered: list[Any] = []
            for index, item in enumerate(value):
                if max_items is not None and index >= max_items:
                    rendered.append(f"<{len(value) - max_items} additional entries>")
                    break
                rendered.append(
                    self._redact(
                        item,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_items=max_items,
                        ancestors=ancestors,
                        fail_on_depth=fail_on_depth,
                    )
                )
            return rendered
        finally:
            ancestors.remove(identity)


DEFAULT_REDACTION_POLICY = RedactionPolicy()


def _policy(sensitive_keys: tuple[str, ...]) -> RedactionPolicy:
    return RedactionPolicy(sensitive_keys) if sensitive_keys else DEFAULT_REDACTION_POLICY


def is_sensitive_key(key: object, *, sensitive_keys: tuple[str, ...] = ()) -> bool:
    return _policy(sensitive_keys).is_sensitive_key(key)


def contains_sensitive_data(value: str, *, sensitive_keys: tuple[str, ...] = ()) -> bool:
    return _policy(sensitive_keys).contains_sensitive_data(value)


def redact_text(value: str, *, sensitive_keys: tuple[str, ...] = ()) -> str:
    return _policy(sensitive_keys).redact_text(value)


def redact_url(value: str, *, sensitive_keys: tuple[str, ...] = ()) -> str:
    return _policy(sensitive_keys).redact_url(value)


def redact(value: Any, *, sensitive_keys: tuple[str, ...] = ()) -> Any:
    """Return a bounded redacted representation suitable only for logs."""

    return _policy(sensitive_keys).redact_log(value)


def redact_artifact(value: Any, *, sensitive_keys: tuple[str, ...] = ()) -> Any:
    """Return a complete redacted representation suitable for persisted artifacts."""

    return _policy(sensitive_keys).redact_artifact(value)


__all__ = [
    "DEFAULT_REDACTION_POLICY",
    "REDACTED",
    "RedactionPolicy",
    "contains_sensitive_data",
    "is_sensitive_key",
    "redact",
    "redact_artifact",
    "redact_text",
    "redact_url",
]
