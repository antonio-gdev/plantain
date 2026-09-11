"""Typed registry validation without activity execution."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import Field

from plantain.engine.registry import ActivityRegistry
from plantain.errors import ActivityRegistrationError, ActivityValidationError
from plantain.models.common import StrictModel


class ExampleParams(StrictModel):
    id: str
    count: int = Field(ge=1)


async def example_handler(_context: Any, params: ExampleParams) -> int:
    return params.count


def _registry() -> ActivityRegistry:
    registry = ActivityRegistry()
    registry.register(
        "exampleActivity",
        ExampleParams,
        example_handler,
        description="Typed validation test activity",
    )
    return registry


def test_static_validation_accepts_literals_and_exact_typed_expressions() -> None:
    registry = _registry()

    registry.validate("exampleActivity", {"id": "literal", "count": 2})
    registry.validate(
        "exampleActivity",
        {"id": "chained", "count": "${previous.count}"},
    )
    registry.validate(
        "exampleActivity",
        {"id": "generated", "count": "random:int 1 10"},
    )


def test_static_validation_rejects_invalid_literals_missing_and_unknown_fields() -> None:
    registry = _registry()

    with pytest.raises(ActivityValidationError, match="parameters are invalid"):
        registry.validate("exampleActivity", {"id": "bad", "count": "two"})
    with pytest.raises(ActivityValidationError, match="parameters are invalid"):
        registry.validate("exampleActivity", {"id": "missing"})
    with pytest.raises(ActivityValidationError, match="parameters are invalid"):
        registry.validate(
            "exampleActivity",
            {
                "id": "extra",
                "count": 1,
                "unknown": "${previous.value}",
            },
        )


def test_environment_strings_are_not_deferred_for_typed_non_string_fields() -> None:
    registry = _registry()

    with pytest.raises(ActivityValidationError, match="parameters are invalid"):
        registry.validate(
            "exampleActivity",
            {"id": "environment", "count": "env:COUNT"},
        )


def test_distinct_activity_preparers_run_once_before_execution() -> None:
    events: list[str] = []

    async def prepare(_context: Any) -> None:
        events.append("prepared")

    registry = ActivityRegistry()
    for name in ("first", "second"):
        registry.register(
            name,
            ExampleParams,
            example_handler,
            description="Prepared activity",
            preparer=prepare,
        )

    context: Any = object()
    asyncio.run(registry.prepare_many(("first", "second", "first"), context))

    assert events == ["prepared"]


def test_activity_preparer_must_be_async() -> None:
    def invalid_prepare(_context: Any) -> None:
        return None

    registry = ActivityRegistry()
    with pytest.raises(ActivityRegistrationError, match="preparer must be async"):
        registry.register(
            "invalid",
            ExampleParams,
            example_handler,
            description="Invalid prepared activity",
            preparer=invalid_prepare,  # type: ignore[arg-type]
        )
