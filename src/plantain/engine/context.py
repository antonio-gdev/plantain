"""Scenario-scoped data storage with strict path navigation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from plantain.errors import ExpressionResolutionError
from plantain.security.redaction import redact_artifact

MAX_CONTEXT_PATH_LENGTH = 4_096
MAX_CONTEXT_PATH_TOKENS = 128


def _dot_tokens(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    current: list[str] = []
    index = 0
    while index < len(path):
        char = path[index]
        if char == ".":
            if not current:
                raise ExpressionResolutionError(f"Invalid empty path segment in '{path}'")
            tokens.append("".join(current))
            current.clear()
            index += 1
            continue
        if char == "[":
            if current:
                tokens.append("".join(current))
                current.clear()
            end = path.find("]", index + 1)
            if end == -1:
                raise ExpressionResolutionError(f"Unclosed list index in '{path}'")
            raw_index = path[index + 1 : end]
            if not raw_index.isdigit():
                raise ExpressionResolutionError(
                    f"List indexes must be non-negative integers in '{path}'"
                )
            tokens.append(int(raw_index))
            index = end + 1
            if index < len(path) and path[index] == ".":
                index += 1
            continue
        current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    if not tokens or any(isinstance(token, str) and not token for token in tokens):
        raise ExpressionResolutionError(f"Invalid context path: '{path}'")
    return tokens


def _tokens(path: str) -> list[str | int]:
    if len(path) > MAX_CONTEXT_PATH_LENGTH:
        raise ExpressionResolutionError("Context path exceeds its maximum length")
    if "/" not in path:
        tokens = _dot_tokens(path)
    else:
        raw = [part for part in path.strip("/").split("/") if part]
        if not raw:
            raise ExpressionResolutionError(f"Invalid context path: '{path}'")
        tokens = [int(part) if part.isdigit() else part for part in raw]
    if len(tokens) > MAX_CONTEXT_PATH_TOKENS:
        raise ExpressionResolutionError("Context path contains too many segments")
    return tokens


@dataclass(slots=True)
class ScenarioContext:
    """Isolated context passed explicitly through one scenario execution."""

    _store: dict[str, Any] = field(default_factory=dict)
    last_step: str | None = None

    def set_result(self, step_id: str, value: Any) -> None:
        if step_id in self._store:
            raise ExpressionResolutionError(f"A result already exists for step '{step_id}'")
        self._store[step_id] = value
        self.last_step = step_id

    def replace_result(self, step_id: str, value: Any) -> None:
        if step_id not in self._store:
            raise ExpressionResolutionError(f"No result exists for step '{step_id}'")
        self._store[step_id] = value

    def get(self, path: str) -> Any:
        current: Any = self._store
        traversed: list[str] = []
        for token in _tokens(path):
            traversed.append(str(token))
            if isinstance(token, int):
                if not isinstance(current, Sequence) or isinstance(
                    current, (str, bytes, bytearray)
                ):
                    raise ExpressionResolutionError(
                        f"Expected a list at context path '{'/'.join(traversed[:-1])}'"
                    )
                if token >= len(current):
                    raise ExpressionResolutionError(
                        f"List index {token} is out of range at context path '{path}'"
                    )
                current = current[token]
                continue
            if not isinstance(current, Mapping):
                raise ExpressionResolutionError(
                    f"Expected an object at context path '{'/'.join(traversed[:-1])}'"
                )
            if token not in current:
                raise ExpressionResolutionError(
                    f"Context path '{path}' does not contain key '{token}'"
                )
            current = current[token]
        return current

    def contains(self, step_id: str) -> bool:
        return step_id in self._store

    def export(self, *, sanitized: bool = False) -> dict[str, Any]:
        snapshot = deepcopy(self._store)
        return redact_artifact(snapshot) if sanitized else snapshot

    def clear(self) -> None:
        self._store.clear()
        self.last_step = None
