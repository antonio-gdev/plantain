"""Shared Pydantic model configuration."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class StrictModel(BaseModel):
    """Strict boundary model accepting both Python and YAML camel-case names."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class IdentifiedParams(StrictModel):
    """Base parameters for a traceable scenario activity."""

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")


class Operation(StrictModel):
    """A single ordered operation encoded as ``- operationName: value`` in YAML."""

    name: str
    value: Any = None

    @classmethod
    def from_yaml(cls, value: object, *, supported: set[str]) -> Operation:
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("Each operation must be a map containing exactly one operation")
        name, payload = next(iter(value.items()))
        if not isinstance(name, str) or name not in supported:
            available = ", ".join(sorted(supported))
            raise ValueError(f"Unsupported operation '{name}'. Supported operations: {available}")
        return cls(name=name, value=payload)


class TimeoutModel(StrictModel):
    timeout_ms: int | None = Field(default=None, ge=100, le=600_000)


class KeyValue(StrictModel):
    key: str = Field(min_length=1, max_length=256)
    value: Any


_ENV_REFERENCE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")


class EnvironmentReference(StrictModel):
    reference: str

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        if not _ENV_REFERENCE.fullmatch(value):
            raise ValueError("Environment references must use env:VARIABLE_NAME")
        return value
