"""Scenario-scoped secret observation and value-aware output redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from plantain.security.redaction import REDACTED, RedactionPolicy

MAX_OBSERVED_SECRET_VALUES = 1_000
MAX_OBSERVED_SECRET_BYTES = 262_144
MIN_SECRET_LENGTH = 4
_WORD_BOUNDARY_CHARACTERS = "A-Za-z0-9_"


@dataclass(slots=True)
class SecretRegistry:
    """Track sensitive values per scenario without logging or serializing them."""

    sensitive_keys: tuple[str, ...] = field(default_factory=tuple, repr=False)
    _values: set[str] = field(default_factory=set, init=False, repr=False)
    _total_bytes: int = field(default=0, init=False, repr=False)
    _matcher: re.Pattern[str] | None = field(default=None, init=False, repr=False)
    _short_matcher: re.Pattern[str] | None = field(default=None, init=False, repr=False)
    _policy: RedactionPolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._policy = RedactionPolicy(self.sensitive_keys)

    def observe_environment(self, name: str, value: str) -> None:
        if self._policy.is_sensitive_key(name):
            self._add(value)

    def observe(self, value: Any, *, _sensitive: bool = False, _depth: int = 0) -> None:
        """Observe every reachable structured value without silent item limits."""

        del _depth  # Retained for backward-compatible keyword calls.
        pending: list[tuple[Any, bool]] = [(value, _sensitive)]
        visited: set[tuple[int, bool]] = set()
        while pending:
            current, sensitive = pending.pop()
            if current is None or isinstance(current, (bool, int, float)):
                continue
            if isinstance(current, str):
                if sensitive:
                    self._add(current)
                continue
            if isinstance(current, bytes):
                if sensitive:
                    with suppress(UnicodeDecodeError):
                        self._add(current.decode("utf-8"))
                continue
            if hasattr(current, "model_dump"):
                pending.append((current.model_dump(mode="python"), sensitive))
                continue
            if isinstance(current, Mapping):
                marker = (id(current), sensitive)
                if marker in visited:
                    continue
                visited.add(marker)
                pending.extend(
                    (
                        item,
                        sensitive or self._policy.is_sensitive_key(key),
                    )
                    for key, item in current.items()
                )
                continue
            if isinstance(current, Sequence):
                marker = (id(current), sensitive)
                if marker in visited:
                    continue
                visited.add(marker)
                pending.extend((item, sensitive) for item in current)

    def redact(self, value: Any) -> Any:
        """Exhaustively redact a persisted result or artifact."""

        return self._replace(self._policy.redact_artifact(value))

    def redact_log(self, value: Any) -> Any:
        """Redact a bounded log representation with scenario-observed values."""

        return self._replace(self._policy.redact_log(value))

    def redact_log_field(self, field_hint: object, value: Any) -> Any:
        """Redact one log value using its semantic field or locator as context."""

        if self._policy.is_sensitive_reference(field_hint):
            return REDACTED
        return self.redact_log(value)

    def redact_text(self, value: str) -> str:
        return self._replace_text(self._policy.redact_text(value))

    def contains_observed_text(self, value: str) -> bool:
        """Return whether text contains a scenario-observed secret value."""

        if not self._values:
            return False
        self._ensure_matchers()
        long_match = self._matcher is not None and self._matcher.search(value) is not None
        short_match = (
            self._short_matcher is not None and self._short_matcher.search(value) is not None
        )
        return long_match or short_match

    def redact_multiline(self, value: str) -> str:
        """Redact semantic text line-by-line without masking unrelated layout."""

        redacted_lines: list[str] = []
        for line in value.splitlines(keepends=True):
            content = line.rstrip("\r\n")
            line_ending = line[len(content) :]
            redacted_lines.append(self._policy.redact_text(content) + line_ending)
        redacted = "".join(redacted_lines)
        return self._replace_text(redacted)

    def redact_url(self, value: str) -> str:
        return self._replace_text(self._policy.redact_url(value))

    def clear(self) -> None:
        self._values.clear()
        self._total_bytes = 0
        self._matcher = None
        self._short_matcher = None

    def _replace(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._replace_text(value)
        if isinstance(value, dict):
            return {key: self._replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace(item) for item in value]
        return value

    def _replace_text(self, value: str) -> str:
        if value == REDACTED or not self._values:
            return value
        self._ensure_matchers()
        rendered = self._matcher.sub(REDACTED, value) if self._matcher is not None else value
        if self._short_matcher is not None:
            rendered = self._short_matcher.sub(REDACTED, rendered)
        return rendered

    def _ensure_matchers(self) -> None:
        if self._matcher is not None or self._short_matcher is not None or not self._values:
            return
        long_values = [item for item in self._values if len(item) >= MIN_SECRET_LENGTH]
        short_values = [item for item in self._values if len(item) < MIN_SECRET_LENGTH]
        if long_values:
            alternatives = "|".join(
                re.escape(item) for item in sorted(long_values, key=len, reverse=True)
            )
            self._matcher = re.compile(alternatives)
        if short_values:
            alternatives = "|".join(
                re.escape(item) for item in sorted(short_values, key=len, reverse=True)
            )
            self._short_matcher = re.compile(
                rf"(?<![{_WORD_BOUNDARY_CHARACTERS}])(?:{alternatives})"
                rf"(?![{_WORD_BOUNDARY_CHARACTERS}])"
            )

    def _add(self, value: str) -> None:
        if not value or value == REDACTED or value in self._values:
            return
        try:
            value_bytes = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise RuntimeError("Sensitive value registry accepts only valid UTF-8 text") from None
        if (
            len(self._values) >= MAX_OBSERVED_SECRET_VALUES
            or self._total_bytes + value_bytes > MAX_OBSERVED_SECRET_BYTES
        ):
            raise RuntimeError("Sensitive value registry exceeded its fail-closed capacity")
        self._values.add(value)
        self._total_bytes += value_bytes
        self._matcher = None
        self._short_matcher = None


__all__ = ["SecretRegistry"]
