"""Typed registry for framework-owned activity handlers."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

from plantain.errors import ActivityRegistrationError, ActivityValidationError

if TYPE_CHECKING:
    from plantain.engine.runtime import RunContext

ParamsT = TypeVar("ParamsT", bound=BaseModel)
ActivityHandler = Callable[["RunContext", ParamsT], Awaitable[Any]]
ActivityPreparer = Callable[["RunContext"], Awaitable[None]]
_FULL_CONTEXT_VALUE = re.compile(r"^\$\{[^{}]+}$")
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ActivityDefinition:
    """One activity's unique name, parameter schema, and async handler."""

    name: str
    params_model: type[BaseModel]
    handler: ActivityHandler[Any]
    description: str
    preparer: ActivityPreparer | None = None


class ActivityRegistry:
    """Extensible replacement for a hard-coded dispatch switch."""

    def __init__(self) -> None:
        self._definitions: dict[str, ActivityDefinition] = {}

    def register(
        self,
        name: str,
        params_model: type[ParamsT],
        handler: ActivityHandler[ParamsT],
        *,
        description: str,
        preparer: ActivityPreparer | None = None,
    ) -> None:
        if name in self._definitions:
            raise ActivityRegistrationError(f"Activity is already registered: {name}")
        if not inspect.iscoroutinefunction(handler):
            raise ActivityRegistrationError(f"Activity handler must be async: {name}")
        if preparer is not None and not inspect.iscoroutinefunction(preparer):
            raise ActivityRegistrationError(f"Activity preparer must be async: {name}")
        self._definitions[name] = ActivityDefinition(
            name=name,
            params_model=params_model,
            handler=handler,
            description=description,
            preparer=preparer,
        )

    def definition(self, name: str) -> ActivityDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._definitions))
            raise ActivityValidationError(
                f"Unknown activity '{name}'. Registered activities: {available}"
            ) from exc

    async def execute(self, name: str, context: RunContext, raw_params: dict[str, Any]) -> Any:
        definition = self.definition(name)
        params = cast(
            "BaseModel",
            self._validated_params(definition, raw_params, defer_runtime_values=False),
        )
        return await definition.handler(context, params)

    async def prepare_many(self, names: Iterable[str], context: RunContext) -> None:
        """Run each distinct internal activity preparer once in registration order."""

        prepared: set[int] = set()
        for name in names:
            preparer = self.definition(name).preparer
            if preparer is None or id(preparer) in prepared:
                continue
            await preparer(context)
            prepared.add(id(preparer))

    def validate(self, name: str, raw_params: dict[str, Any]) -> None:
        """Validate literals now while deferring exact typed runtime expressions."""

        definition = self.definition(name)
        self._validated_params(definition, raw_params, defer_runtime_values=True)

    @staticmethod
    def _validated_params(
        definition: ActivityDefinition,
        raw_params: dict[str, Any],
        *,
        defer_runtime_values: bool,
    ) -> BaseModel | None:
        try:
            return definition.params_model.model_validate(raw_params)
        except ValidationError as exc:
            details = exc.errors(include_url=False, include_input=False)
            if defer_runtime_values:
                details = [
                    detail for detail in details if not _deferred_runtime_error(raw_params, detail)
                ]
                if not details:
                    return None
            raise ActivityValidationError(
                f"Activity '{definition.name}' parameters are invalid: {details}"
            ) from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def describe(self) -> list[dict[str, str]]:
        return [
            {"name": definition.name, "description": definition.description}
            for definition in sorted(self._definitions.values(), key=lambda item: item.name)
        ]


def _deferred_runtime_error(raw_params: dict[str, Any], detail: Mapping[str, Any]) -> bool:
    if detail.get("type") == "extra_forbidden":
        return False
    location = detail.get("loc")
    if not isinstance(location, tuple) or not location:
        return False
    value: Any = raw_params
    for part in location:
        if isinstance(value, dict):
            next_value = value.get(part, _MISSING)
        elif isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            next_value = value[part]
        else:
            break
        if next_value is _MISSING:
            break
        value = next_value
    if not isinstance(value, str):
        return False
    clean = value.strip()
    return _FULL_CONTEXT_VALUE.fullmatch(clean) is not None or clean.casefold().startswith(
        "random:"
    )
