"""Strict backend models for progressive dashboard UI discovery."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from plantain.models.common import StrictModel
from plantain.models.ui import CapturePageSnapshotParams

MAX_UI_DOM_EXCERPT_BYTES = 65_536
MAX_UI_EVIDENCE_BYTES = 131_072
UI_DISCOVERY_OUTPUT = "discoverySnapshot"
UiEvidenceState = Literal["verified", "diagnostic"]
UiFailureStage = Literal["action", "verification"]
UiRunId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class UiFailureEvidence(StrictModel):
    """Sanitized operation identity for one repairable UI failure."""

    operation_index: int = Field(ge=1)
    operation_total: int = Field(ge=1)
    operation_type: str = Field(min_length=1, max_length=128)
    operation_target: str = Field(min_length=1, max_length=2_048, repr=False)
    operation_error_type: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_position(self) -> Self:
        """Require a possible operation position."""

        if self.operation_index > self.operation_total:
            raise ValueError("UI failure operation position is invalid")
        return self


class UiRunEvidence(StrictModel):
    """Integrity-verified bounded UI evidence retained outside Reflex state."""

    run_id: UiRunId
    scenario: str = Field(min_length=1, max_length=256)
    source_path: str = Field(min_length=1, max_length=2_048)
    step_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    activity: str = Field(min_length=1, max_length=200)
    canonical_file: str = Field(min_length=1, max_length=255)
    evidence_state: UiEvidenceState
    failure_stage: UiFailureStage | None = None
    failure: UiFailureEvidence | None = None
    parameters: CapturePageSnapshotParams = Field(repr=False)
    url: str = Field(max_length=16_384, repr=False)
    page_title: str = Field(max_length=16_384, repr=False)
    content: str = Field(repr=False)
    truncated: bool = False

    @model_validator(mode="after")
    def valid_evidence(self) -> Self:
        """Require internally consistent and byte-bounded UI evidence."""

        is_diagnostic = self.evidence_state == "diagnostic"
        if is_diagnostic != (self.failure_stage is not None):
            raise ValueError("Diagnostic UI evidence requires one failure stage")
        if is_diagnostic != (self.failure is not None):
            raise ValueError("Diagnostic UI evidence requires one failure context")
        if self.parameters.id != self.step_id:
            raise ValueError("UI evidence step parameters do not match")
        if (self.parameters.activity or self.step_id) != self.activity:
            raise ValueError("UI evidence activity parameters do not match")
        if len(self.content.encode()) > MAX_UI_DOM_EXCERPT_BYTES:
            raise ValueError("UI DOM evidence exceeds its byte limit")
        if len(self.model_dump_json().encode()) > MAX_UI_EVIDENCE_BYTES:
            raise ValueError("UI evidence exceeds its byte limit")
        return self


__all__ = [
    "MAX_UI_DOM_EXCERPT_BYTES",
    "MAX_UI_EVIDENCE_BYTES",
    "UI_DISCOVERY_OUTPUT",
    "UiEvidenceState",
    "UiFailureEvidence",
    "UiFailureStage",
    "UiRunEvidence",
    "UiRunId",
]
