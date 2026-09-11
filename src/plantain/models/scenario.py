"""Scenario and step models for the user-facing YAML contract."""

from __future__ import annotations

from typing import Any, Self

from pydantic import AliasChoices, Field, PrivateAttr, field_validator, model_validator

from plantain.models.common import StrictModel

MAX_TAG_LENGTH = 64
MAX_REMOTE_REPORT_METADATA_LENGTH = 128
MAX_SCENARIO_METADATA_ENTRIES = 100
MAX_SCENARIO_STEPS = 1_000
MAX_STEP_PARAMETERS = 1_000


class StepDefinition(StrictModel):
    """Normalized representation of one single-activity YAML step."""

    activity: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    params: dict[str, Any] = Field(max_length=MAX_STEP_PARAMETERS)

    @classmethod
    def from_yaml(cls, value: object) -> StepDefinition:
        if not isinstance(value, dict) or len(value) != 1:
            raise TypeError("Each scenario step must contain exactly one activity mapping")
        activity, params = next(iter(value.items()))
        if not isinstance(activity, str):
            raise TypeError("Activity names must be strings")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise TypeError(f"Activity '{activity}' parameters must be a map")
        return cls(activity=activity, params=params)

    @property
    def step_id(self) -> str | None:
        value = self.params.get("id")
        return value if isinstance(value, str) else None


class ScenarioDefinition(StrictModel):
    """A validated, normalized scenario document."""

    scenario: str = Field(min_length=1, max_length=256)
    jira_ticket: str | None = Field(
        default=None,
        validation_alias=AliasChoices("jira_ticket", "JiraTicket"),
        serialization_alias="JiraTicket",
        max_length=MAX_REMOTE_REPORT_METADATA_LENGTH,
    )
    test_case_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("test_case_key", "testCaseKey"),
        serialization_alias="testCaseKey",
        max_length=MAX_REMOTE_REPORT_METADATA_LENGTH,
    )
    _source_path: str | None = PrivateAttr(default=None)

    test_run_key: str | int | None = Field(
        default=None,
        validation_alias=AliasChoices("test_run_key", "testRunKey"),
        serialization_alias="testRunKey",
    )
    tags: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        max_length=MAX_SCENARIO_METADATA_ENTRIES,
    )
    outputs: dict[str, Any] = Field(default_factory=dict, max_length=100)
    steps: list[StepDefinition] = Field(min_length=1, max_length=MAX_SCENARIO_STEPS)

    @property
    def source_path(self) -> str | None:
        """Project-relative source used only for namespaced runtime artifacts."""

        return self._source_path

    def bind_source(self, source_path: str) -> Self:
        """Attach a loader-validated scenario-relative path."""

        self._source_path = source_path
        return self

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > MAX_TAG_LENGTH for item in value):
            raise ValueError("Tags must be non-empty and at most 64 characters")
        return list(dict.fromkeys(value))

    @field_validator("test_run_key")
    @classmethod
    def bounded_test_run_key(cls, value: str | int | None) -> str | int | None:
        if value is not None and len(str(value)) > MAX_REMOTE_REPORT_METADATA_LENGTH:
            raise ValueError("testRunKey must be at most 128 characters")
        return value

    @model_validator(mode="after")
    def unique_explicit_step_ids(self) -> ScenarioDefinition:
        seen: set[str] = set()
        for step in self.steps:
            step_id = step.step_id
            if step_id is None:
                continue
            if step_id in seen:
                raise ValueError(f"Duplicate step id: {step_id}")
            seen.add(step_id)
        return self
