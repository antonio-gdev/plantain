"""Safe, user-friendly paths for dashboard-generated scenario source."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from plantain.dashboard.agent.models import (
    MAX_SCENARIO_DIRECTORY_HINT_LENGTH,
    AgentCapability,
)
from plantain.errors import PlantainError

MAX_SCENARIO_DIRECTORY_DEPTH = 8
MAX_SCENARIO_DIRECTORY_BYTES = 512
MAX_SCENARIO_SEGMENT_BYTES = 96
MAX_SCENARIO_FILENAME_BYTES = 96
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_DEFAULT_DIRECTORIES: Mapping[AgentCapability, str] = {
    AgentCapability.UI_DISCOVERY: "generated/ui",
    AgentCapability.API_CONTRACT: "generated/api",
    AgentCapability.DATABASE_DISCOVERY: "generated/database",
    AgentCapability.AUTOMATION_GENERATION: "generated/automation",
}


class DashboardScenarioPathError(PlantainError):
    """Raised when a dashboard scenario location is unsafe or unsupported."""


def default_scenario_directory(capability: AgentCapability) -> str:
    """Return the zero-configuration folder for one scenario-producing capability."""

    try:
        return _DEFAULT_DIRECTORIES[capability]
    except KeyError as exc:
        raise DashboardScenarioPathError("The selected workflow cannot save scenario YAML") from exc


def normalize_scenario_directory(
    value: str | None,
    capability: AgentCapability,
) -> str:
    """Normalize a friendly folder while keeping it beneath ``scenarios``."""

    if value is None or not value.strip():
        return default_scenario_directory(capability)
    if len(value) > MAX_SCENARIO_DIRECTORY_HINT_LENGTH or "\x00" in value:
        raise DashboardScenarioPathError("The scenario folder is invalid")
    rendered = value.strip().replace("\\", "/")
    if rendered.startswith(("/", "~")) or _WINDOWS_DRIVE.match(rendered) is not None:
        raise DashboardScenarioPathError(
            "The scenario folder must remain inside the scenarios collection"
        )
    raw_parts = tuple(part.strip() for part in rendered.split("/") if part.strip())
    if not raw_parts or len(raw_parts) > MAX_SCENARIO_DIRECTORY_DEPTH:
        raise DashboardScenarioPathError("The scenario folder is invalid")
    if any(part in {".", ".."} for part in raw_parts):
        raise DashboardScenarioPathError(
            "The scenario folder must remain inside the scenarios collection"
        )
    parts = tuple(_slug_segment(part, MAX_SCENARIO_SEGMENT_BYTES) for part in raw_parts)
    relative = "/".join(parts)
    if len(relative.encode()) > MAX_SCENARIO_DIRECTORY_BYTES:
        raise DashboardScenarioPathError("The scenario folder is too long")
    return relative


def scenario_filename_stem(value: str) -> str:
    """Create a bounded readable filename stem from a validated scenario name."""

    try:
        return _slug_segment(value, MAX_SCENARIO_FILENAME_BYTES)
    except DashboardScenarioPathError:
        return "scenario"


def _slug_segment(value: str, max_bytes: int) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    separator_pending = False
    for character in normalized:
        if character.isalnum():
            if separator_pending and characters:
                characters.append("-")
            characters.append(character)
            separator_pending = False
        elif characters:
            separator_pending = True
    slug = "".join(characters).rstrip("-")
    if not slug:
        raise DashboardScenarioPathError("Scenario folder names must contain letters or numbers")
    if len(slug.encode()) > max_bytes:
        slug = slug.encode()[:max_bytes].decode("utf-8", errors="ignore").rstrip("-")
    if not slug:
        raise DashboardScenarioPathError("The scenario path is invalid")
    return slug


__all__ = [
    "DashboardScenarioPathError",
    "default_scenario_directory",
    "normalize_scenario_directory",
    "scenario_filename_stem",
]
