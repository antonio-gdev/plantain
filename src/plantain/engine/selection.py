"""Activity-agnostic scenario selection by explicit tag predicates."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from plantain.errors import ScenarioLoadError
from plantain.models.scenario import MAX_TAG_LENGTH, ScenarioDefinition

MAX_TAG_FILTERS = 100


@dataclass(frozen=True, slots=True)
class ScenarioTagFilter:
    """Case-insensitive tag predicates with narrow, deterministic semantics."""

    required_all: frozenset[str] = frozenset()
    required_any: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()

    @classmethod
    def create(
        cls,
        *,
        required_all: Iterable[str] = (),
        required_any: Iterable[str] = (),
        excluded: Iterable[str] = (),
    ) -> ScenarioTagFilter:
        all_tags = _normalize(required_all)
        any_tags = _normalize(required_any)
        excluded_tags = _normalize(excluded)
        if len(all_tags | any_tags | excluded_tags) > MAX_TAG_FILTERS:
            raise ValueError(f"Tag filters cannot exceed {MAX_TAG_FILTERS} unique values")
        if conflicts := all_tags & excluded_tags:
            raise ValueError(f"A required tag cannot also be excluded (conflicts={len(conflicts)})")
        return cls(
            required_all=all_tags,
            required_any=any_tags,
            excluded=excluded_tags,
        )

    @property
    def active(self) -> bool:
        return bool(self.required_all or self.required_any or self.excluded)

    def matches(self, scenario: ScenarioDefinition) -> bool:
        tags = {tag.casefold() for tag in scenario.tags}
        return (
            self.required_all.issubset(tags)
            and (not self.required_any or bool(self.required_any & tags))
            and not bool(self.excluded & tags)
        )


def select_scenarios(
    scenarios: Iterable[ScenarioDefinition],
    tag_filter: ScenarioTagFilter,
) -> list[ScenarioDefinition]:
    """Preserve discovery order while applying a bounded tag filter."""

    candidates = list(scenarios)
    if not tag_filter.active:
        return candidates
    selected = [scenario for scenario in candidates if tag_filter.matches(scenario)]
    if not selected:
        raise ScenarioLoadError("No scenarios matched the requested tag filters")
    return selected


def _normalize(values: Iterable[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for raw in values:
        value = raw.strip()
        if not value or len(value) > MAX_TAG_LENGTH:
            raise ValueError(
                f"Tag filters must be non-empty and at most {MAX_TAG_LENGTH} characters"
            )
        normalized.add(value.casefold())
    return frozenset(normalized)


__all__ = ["ScenarioTagFilter", "select_scenarios"]
