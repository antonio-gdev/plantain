"""Safe recursive resolution for env, context, and bounded random expressions."""

from __future__ import annotations

import os
import re
import secrets
import shlex
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from faker import Faker

from plantain.engine.context import ScenarioContext
from plantain.errors import ExpressionResolutionError

_FULL_CONTEXT = re.compile(r"^\$\{([^{}]+)}$")
_ANY_CONTEXT = re.compile(r"\$\{([^{}]+)}")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UUID_PART_COUNT = 1
_INTEGER_PART_COUNT = 3
_CHOICE_INLINE_PART_COUNT = 2
_FORMATTER_PART_COUNT = 2
_MAX_RANDOM_INTEGER_SPAN = 10_000_000_000
_MAX_RANDOM_CHOICES = 1_000
MAX_ENVIRONMENT_VALUE_BYTES = 1_048_576
MAX_EXPRESSION_OUTPUT_BYTES = 1_048_576

_FAKER_PROVIDERS = {
    "address.city": "city",
    "address.postcode": "postcode",
    "address.streetAddress": "street_address",
    "company.name": "company",
    "internet.email": "email",
    "internet.safeEmail": "safe_email",
    "internet.userName": "user_name",
    "name.firstName": "first_name",
    "name.fullName": "name",
    "name.lastName": "last_name",
    "phone.phoneNumber": "phone_number",
}


def _utf8_size(value: str, *, label: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ExpressionResolutionError(f"{label} must be valid UTF-8 text") from None


def _bounded_text(value: str, *, maximum: int, label: str) -> str:
    if _utf8_size(value, label=label) > maximum:
        raise ExpressionResolutionError(f"{label} exceeds its internal byte limit")
    return value


def _bounded_expression_value(value: Any) -> Any:
    if isinstance(value, str):
        return _bounded_text(
            value,
            maximum=MAX_EXPRESSION_OUTPUT_BYTES,
            label="Expanded expression",
        )
    return value


class ExpressionResolver:
    """Resolve supported expressions without evaluation or dynamic method access."""

    def __init__(
        self,
        context: ScenarioContext,
        *,
        random_seed: int | None = None,
        environment_observer: Callable[[str, str], None] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._context = context
        self._environment_observer = environment_observer
        self._environment_values = environment
        self._faker = Faker()
        if random_seed is not None:
            self._faker.seed_instance(random_seed)

    def resolve(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if not isinstance(value, str):
            return value

        return self._resolve_string(value)

    def _resolve_string(self, value: str) -> Any:
        """Resolve one scalar string using only explicit expression grammars."""

        clean = value.strip()
        if clean.lower().startswith("env:"):
            return self._environment(clean[4:])
        if clean.lower().startswith("random:"):
            return _bounded_expression_value(self._random(clean[7:].strip()))

        full = _FULL_CONTEXT.fullmatch(value)
        if full:
            return _bounded_expression_value(self._context.get(full.group(1).strip()))
        if "${" not in value:
            return value

        return self._interpolate_context(value)

    def _interpolate_context(self, value: str) -> str:
        parts: list[str] = []
        total_bytes = 0
        cursor = 0
        for match in _ANY_CONTEXT.finditer(value):
            resolved = self._context.get(match.group(1).strip())
            if isinstance(resolved, (dict, list)):
                raise ExpressionResolutionError(
                    "Objects and lists can only be used when the context expression occupies "
                    "the entire value"
                )
            replacement = "" if resolved is None else str(resolved)
            for part in (value[cursor : match.start()], replacement):
                total_bytes += _utf8_size(part, label="Expanded expression")
                if total_bytes > MAX_EXPRESSION_OUTPUT_BYTES:
                    raise ExpressionResolutionError(
                        "Expanded expression exceeds its internal byte limit"
                    )
                parts.append(part)
            cursor = match.end()
        tail = value[cursor:]
        total_bytes += _utf8_size(tail, label="Expanded expression")
        if total_bytes > MAX_EXPRESSION_OUTPUT_BYTES:
            raise ExpressionResolutionError("Expanded expression exceeds its internal byte limit")
        parts.append(tail)
        return "".join(parts)

    def _environment(self, name: str) -> str:
        key = name.strip()
        if not _ENV_NAME.fullmatch(key):
            raise ExpressionResolutionError("Environment references must use env:VARIABLE_NAME")
        source = os.environ if self._environment_values is None else self._environment_values
        value = source.get(key)
        if value is None:
            raise ExpressionResolutionError(f"Required environment variable is not set: {key}")
        value = _bounded_text(
            value,
            maximum=MAX_ENVIRONMENT_VALUE_BYTES,
            label="Environment value",
        )
        if self._environment_observer is not None:
            self._environment_observer(key, value)
        return value

    def _random(self, expression: str) -> Any:
        try:
            parts = shlex.split(expression)
        except ValueError as exc:
            raise ExpressionResolutionError("Random expression contains invalid quoting") from exc
        if not parts:
            raise ExpressionResolutionError("Random expression cannot be empty")

        operation = parts[0]
        if operation in {"uuid", "uuid4"} and len(parts) == _UUID_PART_COUNT:
            return str(uuid.uuid4())
        if operation == "int" and len(parts) == _INTEGER_PART_COUNT:
            try:
                lower, upper = int(parts[1]), int(parts[2])
            except ValueError as exc:
                raise ExpressionResolutionError("random:int bounds must be integers") from exc
            if lower > upper or upper - lower > _MAX_RANDOM_INTEGER_SPAN:
                raise ExpressionResolutionError(
                    "random:int bounds are invalid or excessively large"
                )
            return lower + secrets.randbelow(upper - lower + 1)
        if operation == "choice" and len(parts) >= _CHOICE_INLINE_PART_COUNT:
            options = parts[1:] if len(parts) > _CHOICE_INLINE_PART_COUNT else parts[1].split("|")
            options = [item for item in options if item]
            if not options or len(options) > _MAX_RANDOM_CHOICES:
                raise ExpressionResolutionError("random:choice requires between 1 and 1000 values")
            return secrets.choice(options)
        if operation in {"numerify", "lexify", "bothify"} and len(parts) == _FORMATTER_PART_COUNT:
            formatter = getattr(self._faker, operation)
            return formatter(text=parts[1])
        provider = _FAKER_PROVIDERS.get(operation)
        if provider and len(parts) == _UUID_PART_COUNT:
            return getattr(self._faker, provider)()
        available = ", ".join(
            sorted(
                {
                    *_FAKER_PROVIDERS,
                    "uuid",
                    "int",
                    "choice",
                    "numerify",
                    "lexify",
                    "bothify",
                }
            )
        )
        raise ExpressionResolutionError(
            f"Unsupported random expression '{operation}'. Supported generators: {available}"
        )
