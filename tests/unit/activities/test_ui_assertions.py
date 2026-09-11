"""Complete dispatch coverage for hard generic UI verifications."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities import ui_assertions
from plantain.activities.ui_assertions import UiAssertionEngine
from plantain.activities.ui_errors import UiAssertionError
from plantain.models.ui import LocatorSpec, UiAssertion, UiAssertionType

DEFAULT_TIMEOUT_MS = 1_000
CUSTOM_TIMEOUT_MS = 2_000
EXPECTED_COUNT = 2


@dataclass(frozen=True)
class Subject:
    identity: str


class FakeResolver:
    def __init__(
        self,
        locator: Subject,
        *,
        error: Exception | None = None,
    ) -> None:
        self.locator = locator
        self.error = error
        self.resolve_calls: list[tuple[object, LocatorSpec, int | None]] = []
        self.build_calls: list[tuple[object, LocatorSpec]] = []

    async def resolve_one(
        self,
        page: object,
        target: LocatorSpec,
        *,
        timeout_ms: int | None = None,
    ) -> Any:
        self.resolve_calls.append((page, target, timeout_ms))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(locator=self.locator)

    async def build(self, page: object, target: LocatorSpec) -> Any:
        self.build_calls.append((page, target))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(locator=self.locator)


class Expectation:
    def __init__(
        self,
        subject: object,
        calls: list[tuple[str, object, tuple[object, ...], dict[str, object]]],
        *,
        fail_method: str | None = None,
    ) -> None:
        self.subject = subject
        self.calls = calls
        self.fail_method = fail_method

    def __getattr__(self, method: str) -> Any:
        async def invoke(*args: object, **options: object) -> None:
            self.calls.append((method, self.subject, args, options))
            if method == self.fail_method:
                raise RuntimeError("synthetic expectation detail")

        return invoke


def _install_expect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_method: str | None = None,
) -> list[tuple[str, object, tuple[object, ...], dict[str, object]]]:
    calls: list[tuple[str, object, tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        ui_assertions,
        "expect",
        lambda subject: Expectation(subject, calls, fail_method=fail_method),
    )
    return calls


def _target() -> LocatorSpec:
    return LocatorSpec(css="#target")


def test_page_url_and_title_assertions_support_exact_and_contains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_expect(monkeypatch)
    resolver = FakeResolver(Subject("unused"))
    page = Subject("page")
    engine = UiAssertionEngine(cast("Any", resolver), DEFAULT_TIMEOUT_MS)

    asyncio.run(
        engine.verify(
            cast("Any", page),
            UiAssertion(
                assertion=UiAssertionType.URL,
                equals="https://example.test/complete",
            ),
        )
    )
    asyncio.run(
        engine.verify(
            cast("Any", page),
            UiAssertion(
                assertion=UiAssertionType.TITLE,
                contains="Checkout (Complete)",
                timeout_ms=CUSTOM_TIMEOUT_MS,
            ),
        )
    )

    assert calls[0] == (
        "to_have_url",
        page,
        ("https://example.test/complete",),
        {"timeout": DEFAULT_TIMEOUT_MS},
    )
    title_pattern = calls[1][2][0]
    assert isinstance(title_pattern, re.Pattern)
    assert title_pattern.pattern == re.escape("Checkout (Complete)")
    assert calls[1][3] == {"timeout": CUSTOM_TIMEOUT_MS}
    assert resolver.resolve_calls == []
    assert resolver.build_calls == []


@pytest.mark.parametrize(
    ("assertion_type", "expected_method", "uses_build"),
    [
        (UiAssertionType.VISIBLE, "to_be_visible", False),
        (UiAssertionType.HIDDEN, "to_be_hidden", True),
        (UiAssertionType.ENABLED, "to_be_enabled", False),
        (UiAssertionType.DISABLED, "to_be_disabled", False),
        (UiAssertionType.EDITABLE, "to_be_editable", False),
        (UiAssertionType.CHECKED, "to_be_checked", False),
        (UiAssertionType.UNCHECKED, "not_to_be_checked", False),
    ],
)
def test_state_assertions_dispatch_to_playwright_expectations(
    assertion_type: UiAssertionType,
    expected_method: str,
    uses_build: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_expect(monkeypatch)
    locator = Subject("locator")
    resolver = FakeResolver(locator)
    page = Subject("page")

    asyncio.run(
        UiAssertionEngine(cast("Any", resolver), DEFAULT_TIMEOUT_MS).verify(
            cast("Any", page),
            UiAssertion(assertion=assertion_type, target=_target()),
        )
    )

    assert calls == [(expected_method, locator, (), {"timeout": DEFAULT_TIMEOUT_MS})]
    if uses_build:
        assert resolver.build_calls == [(page, _target())]
        assert resolver.resolve_calls == []
    else:
        assert resolver.resolve_calls == [(page, _target(), DEFAULT_TIMEOUT_MS)]
        assert resolver.build_calls == []


@pytest.mark.parametrize(
    ("assertion", "expected_method", "uses_build"),
    [
        (
            UiAssertion(
                assertion=UiAssertionType.TEXT,
                target=_target(),
                contains="Complete",
            ),
            "to_contain_text",
            False,
        ),
        (
            UiAssertion(
                assertion=UiAssertionType.TEXT,
                target=_target(),
                equals="Complete",
            ),
            "to_have_text",
            False,
        ),
        (
            UiAssertion(
                assertion=UiAssertionType.VALUE,
                target=_target(),
                contains="90210",
            ),
            "to_have_value",
            False,
        ),
        (
            UiAssertion(
                assertion=UiAssertionType.VALUE,
                target=_target(),
                equals="90210",
            ),
            "to_have_value",
            False,
        ),
        (
            UiAssertion(
                assertion=UiAssertionType.COUNT,
                target=_target(),
                equals=EXPECTED_COUNT,
            ),
            "to_have_count",
            True,
        ),
        (
            UiAssertion(
                assertion=UiAssertionType.ATTRIBUTE,
                target=_target(),
                attribute="data-state",
                equals="ready",
            ),
            "to_have_attribute",
            False,
        ),
    ],
)
def test_content_assertions_dispatch_exactly(
    assertion: UiAssertion,
    expected_method: str,
    uses_build: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_expect(monkeypatch)
    locator = Subject("locator")
    resolver = FakeResolver(locator)
    page = Subject("page")

    asyncio.run(
        UiAssertionEngine(cast("Any", resolver), DEFAULT_TIMEOUT_MS).verify(
            cast("Any", page), assertion
        )
    )

    assert calls[0][0] == expected_method
    assert calls[0][1] is locator
    assert calls[0][3] == {"timeout": DEFAULT_TIMEOUT_MS}
    if assertion.assertion is UiAssertionType.VALUE and assertion.contains is not None:
        value_pattern = calls[0][2][0]
        assert isinstance(value_pattern, re.Pattern)
        assert value_pattern.pattern == re.escape(assertion.contains)
    elif assertion.assertion is UiAssertionType.ATTRIBUTE:
        assert calls[0][2] == ("data-state", "ready")
    elif assertion.assertion is UiAssertionType.COUNT:
        assert calls[0][2] == (EXPECTED_COUNT,)
    else:
        expected_value = assertion.contains or str(assertion.equals)
        assert calls[0][2] == (expected_value,)
    assert bool(resolver.build_calls) is uses_build
    assert bool(resolver.resolve_calls) is not uses_build


def test_engine_defensively_rejects_invalid_count_missing_target_and_unknown_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_expect(monkeypatch)
    engine = UiAssertionEngine(
        cast("Any", FakeResolver(Subject("locator"))),
        DEFAULT_TIMEOUT_MS,
    )
    invalid_count = UiAssertion.model_construct(
        assertion=UiAssertionType.COUNT,
        target=_target(),
        equals=True,
    )
    missing_target = UiAssertion.model_construct(assertion=UiAssertionType.VISIBLE)
    unsupported = UiAssertion.model_construct(
        assertion=cast("Any", "unsupported"),
        target=_target(),
    )

    with pytest.raises(UiAssertionError, match="integer expectation"):
        asyncio.run(engine.verify(cast("Any", Subject("page")), invalid_count))
    with pytest.raises(UiAssertionError, match="requires a target"):
        asyncio.run(engine.verify(cast("Any", Subject("page")), missing_target))
    with pytest.raises(UiAssertionError, match="Unsupported UI assertion"):
        asyncio.run(engine.verify(cast("Any", Subject("page")), unsupported))


def test_engine_preserves_ui_errors_and_wraps_unexpected_expectation_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = UiAssertionError("safe verification failure")
    assertion = UiAssertion(assertion=UiAssertionType.VISIBLE, target=_target())

    with pytest.raises(UiAssertionError) as preserved:
        asyncio.run(
            UiAssertionEngine(
                cast("Any", FakeResolver(Subject("locator"), error=expected)),
                DEFAULT_TIMEOUT_MS,
            ).verify(cast("Any", Subject("page")), assertion)
        )
    assert preserved.value is expected

    _install_expect(monkeypatch, fail_method="to_be_visible")
    with pytest.raises(
        UiAssertionError,
        match="UI verification 'visible' failed",
    ) as wrapped:
        asyncio.run(
            UiAssertionEngine(
                cast("Any", FakeResolver(Subject("locator"))),
                DEFAULT_TIMEOUT_MS,
            ).verify(cast("Any", Subject("page")), assertion)
        )
    assert isinstance(wrapped.value.__cause__, RuntimeError)
    assert "synthetic expectation detail" not in str(wrapped.value)
