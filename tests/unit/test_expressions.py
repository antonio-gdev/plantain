"""Bounded expression resolution without dynamic evaluation."""

from __future__ import annotations

from uuid import UUID

import pytest

from plantain.engine import expressions
from plantain.engine.context import ScenarioContext
from plantain.engine.expressions import ExpressionResolver
from plantain.errors import ExpressionResolutionError

RANDOM_UPPER_BOUND = 8


def _context() -> ScenarioContext:
    context = ScenarioContext()
    context.set_result(
        "lookup",
        {"count": 2, "empty": None, "record": {"item": "Jacket"}},
    )
    return context


def test_resolver_handles_recursive_values_environment_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str]] = []
    monkeypatch.setenv("APP_LABEL", "catalog")
    resolver = ExpressionResolver(
        _context(),
        environment_observer=lambda key, value: observed.append((key, value)),
    )

    resolved = resolver.resolve(
        {
            7: [
                "env:APP_LABEL",
                "${lookup.count}",
                "items=${lookup.count}",
                "empty=${lookup.empty}",
            ],
            "record": "${lookup.record}",
            "unchanged": 42,
        }
    )

    assert resolved == {
        "7": ["catalog", 2, "items=2", "empty="],
        "record": {"item": "Jacket"},
        "unchanged": 42,
    }
    assert observed == [("APP_LABEL", "catalog")]


@pytest.mark.parametrize("reference", ["env:BAD-NAME", "env:", "env:9INVALID"])
def test_resolver_rejects_invalid_environment_names(reference: str) -> None:
    with pytest.raises(ExpressionResolutionError, match="env:VARIABLE_NAME"):
        ExpressionResolver(_context()).resolve(reference)


def test_resolver_rejects_missing_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_VALUE", raising=False)

    with pytest.raises(ExpressionResolutionError, match="MISSING_VALUE"):
        ExpressionResolver(_context()).resolve("env:MISSING_VALUE")


def test_resolver_can_use_an_explicit_environment_mapping() -> None:
    observed: list[tuple[str, str]] = []
    resolver = ExpressionResolver(
        _context(),
        environment_observer=lambda key, value: observed.append((key, value)),
        environment={"APP_LABEL": "catalog"},
    )

    assert resolver.resolve("env:APP_LABEL") == "catalog"
    assert observed == [("APP_LABEL", "catalog")]
    with pytest.raises(ExpressionResolutionError, match="MISSING_VALUE"):
        resolver.resolve("env:MISSING_VALUE")


def test_resolver_bounds_environment_and_expanded_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(expressions, "MAX_ENVIRONMENT_VALUE_BYTES", 4)
    monkeypatch.setenv("APP_PASSWORD", "12345")
    resolver = ExpressionResolver(
        _context(),
        environment_observer=lambda key, value: observed.append((key, value)),
    )

    with pytest.raises(ExpressionResolutionError, match="Environment value exceeds"):
        resolver.resolve("env:APP_PASSWORD")
    assert observed == []

    context = ScenarioContext()
    context.set_result("step", {"value": "12345"})
    monkeypatch.setattr(expressions, "MAX_EXPRESSION_OUTPUT_BYTES", 4)
    resolver = ExpressionResolver(context)

    with pytest.raises(ExpressionResolutionError, match="Expanded expression exceeds"):
        resolver.resolve("${step.value}")
    with pytest.raises(ExpressionResolutionError, match="Expanded expression exceeds"):
        resolver.resolve("x=${step.value}")
    assert resolver.resolve("ordinary literal input") == "ordinary literal input"


def test_resolver_rejects_embedded_objects_and_missing_context_paths() -> None:
    resolver = ExpressionResolver(_context())

    with pytest.raises(ExpressionResolutionError, match="Objects and lists"):
        resolver.resolve("record=${lookup.record}")
    with pytest.raises(ExpressionResolutionError, match="does not contain key"):
        resolver.resolve("${lookup.missing}")


def test_random_uuid_integer_and_choice_are_bounded_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uuid_value = UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(expressions.uuid, "uuid4", lambda: uuid_value)
    monkeypatch.setattr(expressions.secrets, "randbelow", lambda span: span - 1)
    monkeypatch.setattr(expressions.secrets, "choice", lambda options: options[-1])
    resolver = ExpressionResolver(_context())

    assert resolver.resolve("random:uuid") == str(uuid_value)
    assert resolver.resolve("random:uuid4") == str(uuid_value)
    assert resolver.resolve(f"random:int 4 {RANDOM_UPPER_BOUND}") == RANDOM_UPPER_BOUND
    assert resolver.resolve("random:choice red|green|blue") == "blue"
    assert resolver.resolve("random:choice red green blue") == "blue"


def test_seeded_faker_and_formatters_are_repeatable() -> None:
    first = ExpressionResolver(_context(), random_seed=17)
    second = ExpressionResolver(_context(), random_seed=17)
    generators = (
        "random:name.firstName",
        "random:address.city",
        "random:numerify '##-##'",
        "random:lexify '??'",
        "random:bothify '?#'",
    )

    assert [first.resolve(item) for item in generators] == [
        second.resolve(item) for item in generators
    ]


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("random:", "cannot be empty"),
        ("random:'unterminated", "invalid quoting"),
        ("random:int low 2", "bounds must be integers"),
        ("random:int 3 2", "bounds are invalid"),
        ("random:int 0 10000000001", "bounds are invalid"),
        ("random:choice ''", "between 1 and 1000"),
        ("random:uuid extra", "Unsupported random expression"),
        ("random:unknown", "Unsupported random expression"),
    ],
)
def test_resolver_rejects_malformed_random_expressions(
    expression: str,
    message: str,
) -> None:
    with pytest.raises(ExpressionResolutionError, match=message):
        ExpressionResolver(_context()).resolve(expression)


def test_random_choice_rejects_excessive_option_count() -> None:
    expression = "random:choice " + " ".join(f"value-{index}" for index in range(1_001))

    with pytest.raises(ExpressionResolutionError, match="between 1 and 1000"):
        ExpressionResolver(_context()).resolve(expression)
