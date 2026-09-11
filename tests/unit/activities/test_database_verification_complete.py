"""Complete typed database-result verification coverage."""

from __future__ import annotations

from typing import Any

import pytest

from plantain.activities.database import verification
from plantain.activities.database.errors import DatabaseActivityError
from plantain.activities.database.verification import (
    _exact,
    _is_sequence,
    _unwrap,
    verify_result,
)
from plantain.models.database import VerifyDatabaseResult, VerifyDatabaseResultParams

EXPECTED_MATCHED_ROWS = 2
MULTISET_ROW_COUNT = 2_000


def _result(actual: Any, expected: Any, *, mode: str = "contains") -> VerifyDatabaseResult:
    return verify_result(
        VerifyDatabaseResultParams.model_validate(
            {
                "id": "verify_database",
                "actual": actual,
                "expected": expected,
                "matchMode": mode,
            }
        )
    )


def _failure(actual: Any, expected: Any, *, mode: str = "contains") -> str:
    with pytest.raises(DatabaseActivityError) as captured:
        _result(actual, expected, mode=mode)
    return str(captured.value)


def test_success_reports_scalar_and_sequence_match_counts() -> None:
    scalar = _result("ready", "ready", mode="exact")
    rows = _result(
        [{"id": 1}, {"id": 2}, {"id": 3}],
        [{"id": 1}, {"id": 2}],
    )

    assert scalar.matched_rows == 1
    assert rows.matched_rows == EXPECTED_MATCHED_ROWS
    assert rows.match_mode.value == "contains"


def test_result_wrapper_unwraps_only_complete_runtime_shape() -> None:
    assert _unwrap({"result": "ready", "truncated": False}) == ("ready", False)
    assert _unwrap({"result": "ready", "truncated": 1}) == ("ready", False)
    partial = {"result": "ready"}
    assert _unwrap(partial) == (partial, False)


def test_exact_reports_mapping_key_and_nested_value_mismatches() -> None:
    assert "mapping keys differ" in _failure(
        {"id": 1, "state": "ready"},
        {"id": 1},
        mode="exact",
    )
    nested = _failure(
        {"item": {"state": "ready"}},
        {"item": {"state": "pending"}},
        mode="exact",
    )
    assert "$.item.state" in nested
    assert "value differs" in nested
    assert "ready" not in nested
    assert "pending" not in nested


def test_exact_reports_sequence_item_path_and_accepts_equal_nested_values() -> None:
    assert (
        _exact(
            [{"id": 1}, {"id": 2}],
            [{"id": 1}, {"id": 2}],
            path="$",
        )
        is None
    )

    message = _failure(
        [{"id": 1}, {"id": 2}],
        [{"id": 1}, {"id": 3}],
        mode="exact",
    )
    assert "$[1].id" in message
    assert "value differs" in message


@pytest.mark.parametrize(
    "actual, expected, reason, path",
    [
        ("not-a-map", {"id": 1}, "expected a mapping", "$"),
        ({"state": "ready"}, {"id": 1}, "key is missing", "$.id"),
        ("not-a-list", [1], "expected a sequence", "$"),
        ([1], [1, 2], "actual sequence is shorter", "$"),
        ([1, 2], [1, 3], "value differs", "$[1]"),
        (1, True, "type differs", "$"),
        (1, 2, "value differs", "$"),
    ],
)
def test_contains_reports_each_structural_mismatch_without_values(
    actual: Any,
    expected: Any,
    reason: str,
    path: str,
) -> None:
    message = _failure(actual, expected)

    assert reason in message
    assert path in message


def test_unordered_contains_requires_sequences_and_reports_missing_match() -> None:
    assert "unorderedContains requires sequences" in _failure(
        {"id": 1},
        [{"id": 1}],
        mode="unorderedContains",
    )
    message = _failure(
        [{"id": 1}],
        [{"id": 2}],
        mode="unorderedContains",
    )
    assert "$[0]" in message
    assert "no matching row was found" in message


def test_unordered_contains_consumes_each_actual_row_once() -> None:
    message = _failure(
        [{"id": 1}],
        [{"id": 1}, {"id": 1}],
        mode="unorderedContains",
    )

    assert "$[1]" in message
    assert "no matching row was found" in message


def test_unordered_contains_reassigns_overlapping_partial_rows() -> None:
    result = _result(
        [
            {"id": 1, "state": "ready"},
            {"id": 1},
        ],
        [
            {"id": 1},
            {"id": 1, "state": "ready"},
        ],
        mode="unorderedContains",
    )

    assert result.matched_rows == EXPECTED_MATCHED_ROWS


def test_unordered_contains_uses_multiset_for_uniform_partial_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_fallback(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("uniform rows must not use pairwise matching")

    monkeypatch.setattr(verification, "_bounded_unordered_match", reject_fallback)
    actual = [{"id": index, "retained": f"value-{index}"} for index in range(MULTISET_ROW_COUNT)]
    expected = [{"id": index} for index in reversed(range(MULTISET_ROW_COUNT))]

    result = _result(actual, expected, mode="unorderedContains")

    assert result.matched_rows == MULTISET_ROW_COUNT


def test_heterogeneous_unordered_comparison_has_safe_work_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification, "MAX_UNORDERED_MATCH_WORK", 1)

    message = _failure(
        [{"id": 1}, {"state": "ready"}],
        [{}, {"id": 1}],
        mode="unorderedContains",
    )

    assert "safe work limit" in message
    assert "ready" not in message


@pytest.mark.parametrize(
    "value, expected",
    [
        ([1], True),
        ((1,), True),
        ("value", False),
        (b"value", False),
        (bytearray(b"value"), False),
        ({"id": 1}, False),
    ],
)
def test_sequence_classification_excludes_scalar_and_mapping_values(
    value: Any,
    expected: bool,
) -> None:
    assert _is_sequence(value) is expected
