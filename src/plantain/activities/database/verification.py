"""Typed database-result assertions that never open a database connection."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from plantain.activities.database.errors import DatabaseActivityError
from plantain.models.database import (
    DatabaseMatchMode,
    VerifyDatabaseResult,
    VerifyDatabaseResultParams,
)

MAX_UNORDERED_MATCH_WORK = 1_000_000
_NO_PROJECTION = object()


class _ProjectionUnavailableError(Exception):
    """Signal that a value cannot safely form an internal multiset key."""


@dataclass(frozen=True, slots=True)
class _Mismatch:
    path: str
    reason: str


@dataclass(slots=True)
class _MatchWork:
    remaining: int = MAX_UNORDERED_MATCH_WORK

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise DatabaseActivityError(
                "Unordered database comparison exceeded its safe work limit; "
                "use consistent row shapes or a narrower expected set"
            )


def verify_result(params: VerifyDatabaseResultParams) -> VerifyDatabaseResult:
    """Verify an already chained value without logging its potentially sensitive data."""

    actual, truncated = _unwrap(params.actual)
    if truncated:
        raise DatabaseActivityError(
            "Cannot verify a truncated database result; narrow the query first"
        )
    if params.match_mode is DatabaseMatchMode.EXACT:
        mismatch = _exact(actual, params.expected, path="$")
    elif params.match_mode is DatabaseMatchMode.UNORDERED_CONTAINS:
        mismatch = _unordered_contains(actual, params.expected, path="$")
    else:
        mismatch = _contains(actual, params.expected, path="$")
    if mismatch is not None:
        raise DatabaseActivityError(
            f"Database result verification failed at {mismatch.path}: {mismatch.reason}"
        )
    matched_rows = len(params.expected) if _is_sequence(params.expected) else 1
    return VerifyDatabaseResult(
        match_mode=params.match_mode,
        matched_rows=matched_rows,
    )


def _unwrap(value: Any) -> tuple[Any, bool]:
    if isinstance(value, Mapping) and {"result", "truncated"}.issubset(value):
        return value["result"], value["truncated"] is True
    return value, False


def _exact(  # noqa: PLR0911
    actual: Any,
    expected: Any,
    *,
    path: str,
) -> _Mismatch | None:
    if type(actual) is not type(expected):
        return _Mismatch(path, "type differs")
    if isinstance(expected, Mapping):
        if set(actual) != set(expected):
            return _Mismatch(path, "mapping keys differ")
        for key, expected_value in expected.items():
            mismatch = _exact(actual[key], expected_value, path=f"{path}.{key}")
            if mismatch is not None:
                return mismatch
        return None
    if _is_sequence(expected):
        if len(actual) != len(expected):
            return _Mismatch(path, "sequence length differs")
        for index, (actual_value, expected_value) in enumerate(zip(actual, expected, strict=True)):
            mismatch = _exact(actual_value, expected_value, path=f"{path}[{index}]")
            if mismatch is not None:
                return mismatch
        return None
    return None if actual == expected else _Mismatch(path, "value differs")


def _contains(  # noqa: PLR0911
    actual: Any,
    expected: Any,
    *,
    path: str,
) -> _Mismatch | None:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return _Mismatch(path, "expected a mapping")
        for key, expected_value in expected.items():
            if key not in actual:
                return _Mismatch(f"{path}.{key}", "key is missing")
            mismatch = _contains(actual[key], expected_value, path=f"{path}.{key}")
            if mismatch is not None:
                return mismatch
        return None
    if _is_sequence(expected):
        if not _is_sequence(actual):
            return _Mismatch(path, "expected a sequence")
        if len(actual) < len(expected):
            return _Mismatch(path, "actual sequence is shorter than expected")
        for index, expected_value in enumerate(expected):
            mismatch = _contains(actual[index], expected_value, path=f"{path}[{index}]")
            if mismatch is not None:
                return mismatch
        return None
    if type(actual) is not type(expected):
        return _Mismatch(path, "type differs")
    return None if actual == expected else _Mismatch(path, "value differs")


def _unordered_contains(actual: Any, expected: Any, *, path: str) -> _Mismatch | None:
    if not _is_sequence(actual) or not _is_sequence(expected):
        return _Mismatch(path, "unorderedContains requires sequences")
    if len(actual) < len(expected):
        return _Mismatch(f"{path}[{len(actual)}]", "no matching row was found")
    applicable, mismatch = _multiset_contains(actual, expected, path=path)
    if applicable:
        return mismatch
    return _bounded_unordered_match(actual, expected, path=path)


def _augment_match(
    start: int,
    candidates: Sequence[Sequence[int]],
    left_matches: dict[int, int],
    right_matches: dict[int, int],
    work: _MatchWork,
) -> bool:
    pending = deque([start])
    visited_expected = {start}
    visited_actual: set[int] = set()
    parents: dict[int, int] = {}
    while pending:
        expected_index = pending.popleft()
        for actual_index in candidates[expected_index]:
            work.consume()
            if actual_index in visited_actual:
                continue
            visited_actual.add(actual_index)
            parents[actual_index] = expected_index
            matched_expected = right_matches.get(actual_index)
            if matched_expected is None:
                _apply_augmenting_path(actual_index, parents, left_matches, right_matches)
                return True
            if matched_expected not in visited_expected:
                visited_expected.add(matched_expected)
                pending.append(matched_expected)
    return False


def _apply_augmenting_path(
    free_actual: int,
    parents: Mapping[int, int],
    left_matches: dict[int, int],
    right_matches: dict[int, int],
) -> None:
    actual_index = free_actual
    while True:
        expected_index = parents[actual_index]
        previous_actual = left_matches.get(expected_index)
        left_matches[expected_index] = actual_index
        right_matches[actual_index] = expected_index
        if previous_actual is None:
            return
        actual_index = previous_actual


def _bounded_unordered_match(
    actual: Sequence[Any],
    expected: Sequence[Any],
    *,
    path: str,
) -> _Mismatch | None:
    work = _MatchWork(MAX_UNORDERED_MATCH_WORK)
    candidates: list[list[int]] = []
    for expected_index, expected_value in enumerate(expected):
        matching: list[int] = []
        for actual_index, actual_value in enumerate(actual):
            work.consume()
            if (
                _contains(
                    actual_value,
                    expected_value,
                    path=f"{path}[{expected_index}]",
                )
                is None
            ):
                matching.append(actual_index)
        if not matching:
            return _Mismatch(f"{path}[{expected_index}]", "no matching row was found")
        candidates.append(matching)
    left_matches: dict[int, int] = {}
    right_matches: dict[int, int] = {}
    for expected_index in range(len(expected)):
        if not _augment_match(expected_index, candidates, left_matches, right_matches, work):
            return _Mismatch(f"{path}[{expected_index}]", "no matching row was found")
    return None


def _sequence_projection(actual: Any, template: Sequence[Any]) -> Hashable | object:
    if not _is_sequence(actual) or len(actual) < len(template):
        return _NO_PROJECTION
    values = tuple(
        _comparison_projection(actual[index], expected_value)
        for index, expected_value in enumerate(template)
    )
    return _NO_PROJECTION if _NO_PROJECTION in values else ("sequence", values)


def _scalar_projection(actual: Any, template: Any) -> Hashable | object:
    if type(actual) is not type(template):
        return _NO_PROJECTION
    try:
        hash(actual)
    except TypeError as exc:
        raise _ProjectionUnavailableError from exc
    return ("scalar", cast("Hashable", actual))


def _mapping_projection(
    actual: Any,
    template: Mapping[Any, Any],
) -> Hashable | object:
    if not isinstance(actual, Mapping):
        return _NO_PROJECTION
    projected: list[Hashable] = []
    for key, expected_value in template.items():
        if key not in actual:
            return _NO_PROJECTION
        value = _comparison_projection(actual[key], expected_value)
        if value is _NO_PROJECTION:
            return _NO_PROJECTION
        projected.append(value)
    return ("mapping", tuple(projected))


def _comparison_projection(actual: Any, template: Any) -> Hashable | object:
    if isinstance(template, Mapping):
        return _mapping_projection(actual, template)
    if _is_sequence(template):
        return _sequence_projection(actual, template)
    return _scalar_projection(actual, template)


def _comparison_shape(value: Any) -> Hashable:
    if isinstance(value, Mapping):
        return (
            "mapping",
            tuple((key, _comparison_shape(item)) for key, item in value.items()),
        )
    if _is_sequence(value):
        return ("sequence", tuple(_comparison_shape(item) for item in value))
    return ("scalar", type(value))


def _multiset_contains(
    actual: Sequence[Any],
    expected: Sequence[Any],
    *,
    path: str,
) -> tuple[bool, _Mismatch | None]:
    if not expected:
        return True, None
    template = expected[0]
    shape = _comparison_shape(template)
    if any(_comparison_shape(item) != shape for item in expected[1:]):
        return False, None
    try:
        available: Counter[Hashable] = Counter()
        for item in actual:
            signature = _comparison_projection(item, template)
            if signature is not _NO_PROJECTION:
                available[signature] += 1
        for index, item in enumerate(expected):
            signature = _comparison_projection(item, template)
            if available[signature] == 0:
                return True, _Mismatch(f"{path}[{index}]", "no matching row was found")
            available[signature] -= 1
    except _ProjectionUnavailableError:
        return False, None
    return True, None


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


__all__ = ["verify_result"]
