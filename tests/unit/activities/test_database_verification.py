"""Typed verification tests for chained database values."""

from __future__ import annotations

import pytest

from plantain.activities.database.errors import DatabaseActivityError
from plantain.activities.database.verification import verify_result
from plantain.models.database import VerifyDatabaseResultParams


def _verify(actual: object, expected: object, *, mode: str = "contains") -> None:
    verify_result(
        VerifyDatabaseResultParams.model_validate(
            {
                "id": "verify_database",
                "actual": actual,
                "expected": expected,
                "matchMode": mode,
            }
        )
    )


def test_verifies_scalar_row_and_java_style_ordered_row_prefix() -> None:
    _verify(19.99, 19.99, mode="exact")
    _verify(
        {"Item": "Jacket", "Price": 19.99, "Color": "Blue"},
        {"Item": "Jacket", "Price": 19.99},
    )
    _verify(
        [
            {"Item": "Jacket", "Price": 19.99},
            {"Item": "Coat", "Price": 49.99},
        ],
        [{"Item": "Jacket"}],
    )


def test_exact_and_unordered_modes_are_typed_and_deterministic() -> None:
    _verify(
        [{"Item": "Jacket"}, {"Item": "Coat"}],
        [{"Item": "Coat"}, {"Item": "Jacket"}],
        mode="unorderedContains",
    )
    with pytest.raises(DatabaseActivityError, match="type differs"):
        _verify(1, True, mode="exact")
    with pytest.raises(DatabaseActivityError, match="sequence length differs"):
        _verify([{"Item": "Jacket"}], [], mode="exact")


def test_truncated_results_cannot_be_verified() -> None:
    with pytest.raises(DatabaseActivityError, match="truncated"):
        _verify(
            {
                "result": [{"Item": "Jacket"}],
                "truncated": True,
                "rowCount": 1,
                "resultMode": "rows",
            },
            [{"Item": "Jacket"}],
        )


def test_failure_messages_never_echo_database_values() -> None:
    actual = "actual-sensitive-unit-value"
    expected = "expected-sensitive-unit-value"
    with pytest.raises(DatabaseActivityError) as captured:
        _verify(actual, expected, mode="exact")

    message = str(captured.value)
    assert actual not in message
    assert expected not in message
    assert "value differs" in message
