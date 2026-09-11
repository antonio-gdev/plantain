"""Bounded, duplicate-safe YAML scenario loading."""

from __future__ import annotations

import os
from collections.abc import Iterable
from itertools import islice
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from plantain.config import Settings
from plantain.errors import AtomicPersistenceError, ScenarioLoadError
from plantain.models.scenario import MAX_SCENARIO_STEPS, ScenarioDefinition, StepDefinition
from plantain.persistence import open_binary_read_no_follow

MAX_DISCOVERED_SCENARIO_FILES = 10_000
MAX_SCENARIO_DISCOVERY_ENTRIES = 100_000


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys."""

    def __init__(self, stream: str, *, max_nodes: int, max_depth: int) -> None:
        self._max_nodes = max_nodes
        self._max_depth = max_depth
        self._composed_nodes = 0
        self._composition_depth = 0
        super().__init__(stream)

    def compose_node(
        self,
        parent: yaml.Node | None,
        index: int,
    ) -> yaml.Node | None:
        if self._composition_depth > self._max_depth:
            raise ScenarioLoadError(f"YAML nesting exceeds the maximum depth of {self._max_depth}")
        self._composed_nodes += 1
        if self._composed_nodes > self._max_nodes:
            raise ScenarioLoadError(f"YAML document exceeds the maximum of {self._max_nodes} nodes")
        self._composition_depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._composition_depth -= 1


def _construct_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ScenarioLoadError("YAML mapping keys must be scalar and hashable") from exc
        if duplicate:
            raise ScenarioLoadError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def _enforce_scenario_file_limit(count: int) -> None:
    if count > MAX_DISCOVERED_SCENARIO_FILES:
        raise ScenarioLoadError(
            f"Scenario selection exceeds the maximum of {MAX_DISCOVERED_SCENARIO_FILES} files"
        )


def _resolve_scenario_input(path: Path, settings: Settings) -> tuple[Path, Path]:
    scenario_root = settings.scenarios_dir.resolve()
    supplied = path.expanduser()
    if supplied.is_absolute():
        candidate = supplied
    elif supplied.parts and supplied.parts[0] == settings.scenarios_dir.name:
        candidate = settings.project_root / supplied
    else:
        candidate = scenario_root / supplied
    lexical = Path(os.path.abspath(candidate))  # noqa: PTH100 - preserve symlink identity.
    try:
        lexical_relative = lexical.relative_to(scenario_root)
    except ValueError as exc:
        raise ScenarioLoadError(
            "Scenario inputs must stay inside the configured scenarios directory"
        ) from exc
    cursor = scenario_root
    for part in lexical_relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ScenarioLoadError(f"Scenario inputs cannot contain symlinks: {path}")
    resolved = lexical.resolve()
    try:
        relative = resolved.relative_to(scenario_root)
    except ValueError as exc:
        raise ScenarioLoadError(
            "Scenario inputs must stay inside the configured scenarios directory"
        ) from exc
    return resolved, relative


def _scenario_path(path: Path, settings: Settings) -> tuple[Path, Path]:
    return _resolve_scenario_input(path, settings)


def discover_scenario_paths(
    inputs: Iterable[Path],
    settings: Settings,
    *,
    allow_empty: bool = False,
) -> list[Path]:
    """Resolve files or team directories into a deterministic, safe file set."""

    supplied = list(islice(inputs, MAX_DISCOVERED_SCENARIO_FILES + 1))
    if not supplied:
        raise ScenarioLoadError("At least one scenario file or directory is required")
    _enforce_scenario_file_limit(len(supplied))
    scenario_root = settings.scenarios_dir.resolve()
    discovered: dict[Path, Path] = {}
    for item in supplied:
        resolved, _relative = _resolve_scenario_input(item, settings)
        if not resolved.exists():
            raise ScenarioLoadError(f"Scenario input does not exist: {resolved}")
        if resolved.is_file():
            if resolved.suffix.lower() not in {".yaml", ".yml"}:
                raise ScenarioLoadError("Scenario files must use a .yaml or .yml extension")
            discovered[resolved] = resolved
            _enforce_scenario_file_limit(len(discovered))
            continue
        if not resolved.is_dir():
            raise ScenarioLoadError(f"Scenario input is not a file or directory: {resolved}")
        nested = _discover_directory(resolved, scenario_root)
        if not nested:
            if allow_empty:
                continue
            raise ScenarioLoadError(f"Scenario directory contains no YAML files: {resolved}")
        discovered.update((path, path) for path in nested)
        _enforce_scenario_file_limit(len(discovered))
    return sorted(
        discovered.values(),
        key=lambda path: path.relative_to(scenario_root).as_posix().casefold(),
    )


def _discover_directory(directory: Path, scenario_root: Path) -> list[Path]:
    found: list[Path] = []
    pending = [directory]
    visited_entries = 0
    while pending:
        current = pending.pop()
        try:
            remaining = MAX_SCENARIO_DISCOVERY_ENTRIES - visited_entries
            entries = list(islice(current.iterdir(), remaining + 1))
        except OSError as exc:
            raise ScenarioLoadError(f"Scenario directory is unreadable: {current}") from exc
        visited_entries += len(entries)
        if visited_entries > MAX_SCENARIO_DISCOVERY_ENTRIES:
            raise ScenarioLoadError(
                "Scenario discovery exceeds its internal filesystem-entry limit"
            )
        entries.sort(key=lambda path: path.name.casefold())
        child_directories: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                raise ScenarioLoadError(f"Scenario directories cannot contain symlinks: {entry}")
            if entry.is_dir():
                child_directories.append(entry)
                continue
            if not entry.is_file() or entry.suffix.lower() not in {".yaml", ".yml"}:
                continue
            resolved = entry.resolve()
            try:
                resolved.relative_to(scenario_root)
            except ValueError as exc:
                raise ScenarioLoadError(
                    "Discovered scenario escapes the configured scenarios directory"
                ) from exc
            found.append(resolved)
            _enforce_scenario_file_limit(len(found))
        pending.extend(reversed(child_directories))
    return found


def load_scenario(path: Path, settings: Settings) -> ScenarioDefinition:
    resolved, relative = _scenario_path(path, settings)
    if resolved.suffix.lower() not in {".yaml", ".yml"}:
        raise ScenarioLoadError("Scenario files must use a .yaml or .yml extension")
    if not resolved.exists() or not resolved.is_file():
        raise ScenarioLoadError(f"Scenario file does not exist: {resolved}")
    try:
        with open_binary_read_no_follow(resolved) as handle:
            payload = handle.read(settings.yaml_max_bytes + 1)
    except (OSError, AtomicPersistenceError) as exc:
        raise ScenarioLoadError("Scenario file could not be read safely") from exc
    if len(payload) > settings.yaml_max_bytes:
        raise ScenarioLoadError(
            f"Scenario file exceeds the {settings.yaml_max_bytes}-byte safety limit"
        )
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScenarioLoadError("Scenario must be valid UTF-8") from exc
    return _parse_scenario_text(
        source,
        settings,
        source_path=relative.as_posix(),
    )


def parse_scenario_text(source: str, settings: Settings) -> ScenarioDefinition:
    """Validate one in-memory YAML document through the canonical loader rules."""

    try:
        payload_size = len(source.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ScenarioLoadError("Scenario must be valid UTF-8") from exc
    if payload_size > settings.yaml_max_bytes:
        raise ScenarioLoadError(
            f"Scenario file exceeds the {settings.yaml_max_bytes}-byte safety limit"
        )
    return _parse_scenario_text(source, settings, source_path=None)


def _parse_scenario_text(
    source: str,
    settings: Settings,
    *,
    source_path: str | None,
) -> ScenarioDefinition:
    try:
        loader = UniqueKeyLoader(
            source,
            max_nodes=settings.yaml_max_nodes,
            max_depth=settings.yaml_max_depth,
        )
        try:
            raw = loader.get_single_data()
        finally:
            loader.dispose()
    except yaml.YAMLError as exc:
        raise ScenarioLoadError("Scenario YAML is invalid") from exc
    if not isinstance(raw, dict):
        raise ScenarioLoadError("Scenario root must be a mapping")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        raise ScenarioLoadError("Scenario must contain a steps list")
    if len(raw_steps) > MAX_SCENARIO_STEPS:
        raise ScenarioLoadError(f"Scenario steps exceed the maximum of {MAX_SCENARIO_STEPS}")
    try:
        normalized = dict(raw)
        normalized["steps"] = [StepDefinition.from_yaml(step) for step in raw_steps]
        scenario = ScenarioDefinition.model_validate(normalized)
        return scenario if source_path is None else scenario.bind_source(source_path)
    except (ValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            details = exc.errors(include_url=False, include_input=False)
            raise ScenarioLoadError(f"Scenario validation failed: {details}") from exc
        raise ScenarioLoadError(f"Scenario validation failed: {exc}") from exc
