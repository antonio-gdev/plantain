"""Validated contracts for the generic YAML-facing UI activity."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from plantain.models.api import (
    HTTP_STATUS_MAXIMUM,
    HTTP_STATUS_MINIMUM,
    MAX_EXPECTED_STATUSES,
    HttpMethod,
)


class UiModel(BaseModel):
    """Strict base model with YAML-friendly aliases."""

    model_config = ConfigDict(
        alias_generator=lambda value: "".join(
            [value.split("_")[0], *[part.title() for part in value.split("_")[1:]]]
        ),
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class FrameTarget(UiModel):
    """Identifies an iframe without relying on traversal order."""

    name: str | None = None
    url: str | None = None
    css: str | None = None

    @model_validator(mode="after")
    def require_one_strategy(self) -> Self:
        configured = sum(value is not None for value in (self.name, self.url, self.css))
        if configured != 1:
            raise ValueError("A frame target requires exactly one of name, url, or css")
        return self


class LocatorSpec(UiModel):
    """A semantic-first, deterministic element locator."""

    role: str | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    text: str | None = None
    alt_text: str | None = None
    title: str | None = None
    test_id: str | None = None
    css: str | None = None
    xpath: str | None = None
    exact: bool = True
    nth: int | None = Field(default=None, ge=0)
    visible: bool = True
    frames: list[FrameTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_strategy(self) -> Self:
        strategies = (
            self.role,
            self.label,
            self.placeholder,
            self.text,
            self.alt_text,
            self.title,
            self.test_id,
            self.css,
            self.xpath,
        )
        if sum(value is not None for value in strategies) != 1:
            raise ValueError(
                "A locator requires exactly one primary strategy: role, label, "
                "placeholder, text, altText, title, testId, css, or xpath"
            )
        if self.name is not None and self.role is None:
            raise ValueError("Locator name is only valid together with role")
        return self


class UiActionType(StrEnum):
    CLICK = "click"
    FILL = "fill"
    CLEAR = "clear"
    TYPE = "type"
    PRESS = "press"
    SELECT = "select"
    CHECK = "check"
    UNCHECK = "uncheck"
    HOVER = "hover"
    FOCUS = "focus"
    SCROLL_INTO_VIEW = "scrollIntoView"
    WAIT_FOR = "waitFor"
    WAIT_FOR_URL = "waitForUrl"
    RELOAD = "reload"
    GO_BACK = "goBack"
    GO_FORWARD = "goForward"


class SelectChoice(UiModel):
    """One explicit option selection strategy."""

    value: str | None = None
    label: str | None = None
    index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_one_choice(self) -> Self:
        if sum(value is not None for value in (self.value, self.label, self.index)) != 1:
            raise ValueError("A select choice requires exactly one of value, label, or index")
        return self


class UiResponseExpectation(UiModel):
    """One causal response contract armed before a browser action."""

    url: str = Field(min_length=1, max_length=8_192)
    method: HttpMethod
    status: int | list[int]
    timeout_ms: int | None = Field(default=None, ge=100, le=300_000)

    @field_validator("status")
    @classmethod
    def valid_statuses(cls, value: int | list[int]) -> int | list[int]:
        statuses = (value,) if isinstance(value, int) else tuple(value)
        invalid = any(
            not HTTP_STATUS_MINIMUM <= status <= HTTP_STATUS_MAXIMUM for status in statuses
        )
        if not statuses or len(statuses) > MAX_EXPECTED_STATUSES or invalid:
            raise ValueError("Expected response status must contain 1-100 valid HTTP statuses")
        return value

    @property
    def statuses(self) -> tuple[int, ...]:
        return (self.status,) if isinstance(self.status, int) else tuple(self.status)


class UiAction(UiModel):
    """A normalized browser instruction accepted from declarative YAML."""

    action: UiActionType
    target: LocatorSpec | None = None
    value: str | None = None
    key: str | None = None
    choices: list[SelectChoice] = Field(default_factory=list, max_length=100)
    state: Literal["attached", "detached", "visible", "hidden"] = "visible"
    timeout_ms: int | None = Field(default=None, ge=100, le=300_000)
    delay_ms: int = Field(default=0, ge=0, le=10_000)
    force: bool = False
    expect_popup: bool = False
    expect_response: UiResponseExpectation | None = None

    @classmethod
    def from_yaml(cls, value: Any) -> Self:
        """Accept either ``{action: click, ...}`` or ``{click: {...}}`` syntax."""

        if not isinstance(value, dict):
            raise TypeError("Each UI action must be a mapping")
        if "action" in value:
            return cls.model_validate(value)
        if len(value) != 1:
            raise ValueError("A shorthand UI action must contain exactly one action key")
        action_name, payload = next(iter(value.items()))
        if payload is None:
            payload = {}
        elif not isinstance(payload, dict):
            if action_name in {UiActionType.FILL, UiActionType.TYPE, UiActionType.PRESS}:
                payload = {"value": payload}
            else:
                raise ValueError(f"Action '{action_name}' payload must be a mapping")
        return cls.model_validate({"action": action_name, **payload})

    @model_validator(mode="after")
    def validate_action_arguments(self) -> Self:
        target_actions = {
            UiActionType.CLICK,
            UiActionType.FILL,
            UiActionType.CLEAR,
            UiActionType.TYPE,
            UiActionType.PRESS,
            UiActionType.SELECT,
            UiActionType.CHECK,
            UiActionType.UNCHECK,
            UiActionType.HOVER,
            UiActionType.FOCUS,
            UiActionType.SCROLL_INTO_VIEW,
            UiActionType.WAIT_FOR,
        }
        if self.action in target_actions and self.target is None:
            raise ValueError(f"Action '{self.action}' requires a target")
        if self.action in {UiActionType.FILL, UiActionType.TYPE} and self.value is None:
            raise ValueError(f"Action '{self.action}' requires value")
        if self.action is UiActionType.PRESS and self.key is None and self.value is None:
            raise ValueError("Action 'press' requires key")
        if self.action is UiActionType.SELECT and not self.choices:
            raise ValueError("Action 'select' requires at least one choice")
        if self.action is UiActionType.WAIT_FOR_URL and self.value is None:
            raise ValueError("Action 'waitForUrl' requires value")
        if self.expect_popup and self.action is not UiActionType.CLICK:
            raise ValueError("expectPopup is only valid for click actions")
        return self


class UiAssertionType(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"
    ENABLED = "enabled"
    DISABLED = "disabled"
    EDITABLE = "editable"
    CHECKED = "checked"
    UNCHECKED = "unchecked"
    TEXT = "text"
    VALUE = "value"
    COUNT = "count"
    ATTRIBUTE = "attribute"
    URL = "url"
    TITLE = "title"


class UiAssertion(UiModel):
    """A hard assertion evaluated by the same generic UI activity."""

    assertion: UiAssertionType
    target: LocatorSpec | None = None
    equals: str | int | bool | None = None
    contains: str | None = None
    attribute: str | None = None
    timeout_ms: int | None = Field(default=None, ge=100, le=300_000)

    @classmethod
    def from_yaml(cls, value: Any) -> Self:
        """Accept either normalized or one-key assertion syntax."""

        if not isinstance(value, dict):
            raise TypeError("Each UI verification must be a mapping")
        if "assertion" in value:
            return cls.model_validate(value)
        if len(value) != 1:
            raise ValueError("A shorthand verification must contain exactly one assertion key")
        assertion_name, payload = next(iter(value.items()))
        if payload is None:
            payload = {}
        elif not isinstance(payload, dict):
            payload = {"equals": payload}
        return cls.model_validate({"assertion": assertion_name, **payload})

    @model_validator(mode="after")
    def validate_assertion_arguments(self) -> Self:
        page_assertions = {UiAssertionType.URL, UiAssertionType.TITLE}
        if self.assertion not in page_assertions and self.target is None:
            raise ValueError(f"Assertion '{self.assertion}' requires a target")
        if (
            self.assertion in {UiAssertionType.TEXT, UiAssertionType.VALUE}
            and self.equals is None
            and self.contains is None
        ):
            raise ValueError(f"Assertion '{self.assertion}' requires equals or contains")
        if self.assertion is UiAssertionType.COUNT and not isinstance(self.equals, int):
            raise ValueError("Assertion 'count' requires an integer equals value")
        if self.assertion is UiAssertionType.ATTRIBUTE and (
            self.attribute is None or self.equals is None
        ):
            raise ValueError("Assertion 'attribute' requires attribute and equals")
        if self.assertion in page_assertions and self.equals is None and self.contains is None:
            raise ValueError(f"Assertion '{self.assertion}' requires equals or contains")
        return self


class SnapshotOptions(UiModel):
    """Controls when complete semantic snapshots are captured."""

    enabled: bool = True
    capture_after_each_action: bool = False


class CapturePageSnapshotParams(UiModel):
    """Public contract for the sole YAML-exposed UI activity."""

    id: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    url: str | None = None
    activity: str | None = Field(default=None, max_length=200)
    actions: list[UiAction] = Field(default_factory=list)
    verify: list[UiAssertion] = Field(default_factory=list)
    snapshot: SnapshotOptions = Field(default_factory=SnapshotOptions)
    timeout_ms: int | None = Field(default=None, ge=100, le=300_000)
    wait_until: str = Field(
        default="domcontentloaded",
        pattern=r"^(commit|domcontentloaded|load|networkidle)$",
    )

    @field_validator("actions", mode="before")
    @classmethod
    def parse_actions(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("actions must be a list")
        return [UiAction.from_yaml(item) for item in value]

    @field_validator("verify", mode="before")
    @classmethod
    def parse_assertions(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("verify must be a list")
        return [UiAssertion.from_yaml(item) for item in value]

    @model_validator(mode="after")
    def require_work(self) -> Self:
        if self.url is None and not self.actions and not self.verify:
            raise ValueError("capturePageSnapshot requires url, actions, or verify")
        return self


class SnapshotArtifact(UiModel):
    """One verified or diagnostic snapshot produced by the activity."""

    activity: str
    canonical_file: str
    status: str
    evidence_state: Literal["verified", "diagnostic"] = "verified"
    failure_stage: Literal["action", "verification"] | None = None

    @model_validator(mode="after")
    def require_consistent_evidence_state(self) -> Self:
        is_diagnostic = self.evidence_state == "diagnostic"
        if is_diagnostic != (self.failure_stage is not None):
            raise ValueError("Diagnostic snapshot state requires exactly one failure stage")
        return self


class CapturePageSnapshotResult(UiModel):
    """Compact, context-safe result returned to subsequent YAML steps."""

    success: bool = True
    url: str
    page_title: str
    actions_executed: int
    verifications_passed: int
    snapshots: list[SnapshotArtifact] = Field(default_factory=list)
