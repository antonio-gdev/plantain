"""Complete dispatch coverage for the bounded generic UI action vocabulary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plantain.activities.ui_actions import ActionOutcome, UiActionExecutor
from plantain.activities.ui_errors import UiActionError
from plantain.models.ui import LocatorSpec, SelectChoice, UiAction, UiActionType

DEFAULT_TIMEOUT_MS = 1_000
CUSTOM_TIMEOUT_MS = 2_000
TYPE_DELAY_MS = 25


class FakeLocator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def _record(
        self,
        method: str,
        *args: object,
        **options: object,
    ) -> None:
        self.calls.append((method, args, options))

    async def click(self, **options: object) -> None:
        await self._record("click", **options)

    async def fill(self, value: str, **options: object) -> None:
        await self._record("fill", value, **options)

    async def clear(self, **options: object) -> None:
        await self._record("clear", **options)

    async def press_sequentially(self, value: str, **options: object) -> None:
        await self._record("type", value, **options)

    async def press(self, value: str, **options: object) -> None:
        await self._record("press", value, **options)

    async def select_option(self, **options: object) -> None:
        await self._record("select", **options)

    async def check(self, **options: object) -> None:
        await self._record("check", **options)

    async def uncheck(self, **options: object) -> None:
        await self._record("uncheck", **options)

    async def hover(self, **options: object) -> None:
        await self._record("hover", **options)

    async def focus(self, **options: object) -> None:
        await self._record("focus", **options)

    async def scroll_into_view_if_needed(self, **options: object) -> None:
        await self._record("scroll", **options)

    async def wait_for(self, **options: object) -> None:
        await self._record("wait", **options)


class FakeResolver:
    def __init__(
        self,
        locator: FakeLocator,
        *,
        error: Exception | None = None,
    ) -> None:
        self.locator = locator
        self.error = error
        self.calls: list[tuple[object, LocatorSpec, int | None]] = []

    async def resolve_one(
        self,
        page: object,
        target: LocatorSpec,
        *,
        timeout_ms: int | None = None,
    ) -> Any:
        self.calls.append((page, target, timeout_ms))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(locator=self.locator)


class PopupContext:
    def __init__(self, popup: FakePage) -> None:
        self.popup = popup

    async def __aenter__(self) -> Any:
        async def value() -> FakePage:
            return self.popup

        return SimpleNamespace(value=value())

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None


@dataclass
class FakePage:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = field(default_factory=list)
    popup: FakePage | None = None

    async def wait_for_url(self, value: str, **options: object) -> None:
        self.calls.append(("wait_for_url", (value,), options))

    async def reload(self, **options: object) -> None:
        self.calls.append(("reload", (), options))

    async def go_back(self, **options: object) -> None:
        self.calls.append(("go_back", (), options))

    async def go_forward(self, **options: object) -> None:
        self.calls.append(("go_forward", (), options))

    def expect_popup(self, **options: object) -> PopupContext:
        self.calls.append(("expect_popup", (), options))
        assert self.popup is not None
        return PopupContext(self.popup)

    async def wait_for_load_state(self, value: str, **options: object) -> None:
        self.calls.append(("wait_for_load_state", (value,), options))


def _target() -> LocatorSpec:
    return LocatorSpec(css="#target")


@pytest.mark.parametrize(
    ("action", "expected_call"),
    [
        (
            UiAction(action=UiActionType.WAIT_FOR_URL, value="**/inventory.html"),
            (
                "wait_for_url",
                ("**/inventory.html",),
                {"timeout": DEFAULT_TIMEOUT_MS},
            ),
        ),
        (
            UiAction(action=UiActionType.RELOAD),
            (
                "reload",
                (),
                {"timeout": DEFAULT_TIMEOUT_MS, "wait_until": "domcontentloaded"},
            ),
        ),
        (
            UiAction(action=UiActionType.GO_BACK),
            (
                "go_back",
                (),
                {"timeout": DEFAULT_TIMEOUT_MS, "wait_until": "domcontentloaded"},
            ),
        ),
        (
            UiAction(action=UiActionType.GO_FORWARD, timeout_ms=CUSTOM_TIMEOUT_MS),
            (
                "go_forward",
                (),
                {"timeout": CUSTOM_TIMEOUT_MS, "wait_until": "domcontentloaded"},
            ),
        ),
    ],
)
def test_page_actions_do_not_resolve_element_targets(
    action: UiAction,
    expected_call: tuple[str, tuple[object, ...], dict[str, object]],
) -> None:
    locator = FakeLocator()
    resolver = FakeResolver(locator)
    page = FakePage()

    outcome = asyncio.run(
        UiActionExecutor(cast("Any", resolver), DEFAULT_TIMEOUT_MS).execute(
            cast("Any", page), action
        )
    )

    assert outcome == ActionOutcome(cast("Any", page))
    assert resolver.calls == []
    assert page.calls == [expected_call]


@pytest.mark.parametrize(
    ("action", "expected_call"),
    [
        (
            UiAction(action=UiActionType.FILL, target=_target(), value="Tester"),
            ("fill", ("Tester",), {"timeout": DEFAULT_TIMEOUT_MS, "force": False}),
        ),
        (
            UiAction(action=UiActionType.CLEAR, target=_target(), force=True),
            ("clear", (), {"timeout": DEFAULT_TIMEOUT_MS, "force": True}),
        ),
        (
            UiAction(
                action=UiActionType.TYPE,
                target=_target(),
                value="QA",
                delay_ms=TYPE_DELAY_MS,
            ),
            (
                "type",
                ("QA",),
                {"delay": TYPE_DELAY_MS, "timeout": DEFAULT_TIMEOUT_MS},
            ),
        ),
        (
            UiAction(action=UiActionType.PRESS, target=_target(), key="Enter"),
            ("press", ("Enter",), {"timeout": DEFAULT_TIMEOUT_MS}),
        ),
        (
            UiAction(action=UiActionType.CHECK, target=_target(), force=True),
            ("check", (), {"timeout": DEFAULT_TIMEOUT_MS, "force": True}),
        ),
        (
            UiAction(action=UiActionType.UNCHECK, target=_target()),
            ("uncheck", (), {"timeout": DEFAULT_TIMEOUT_MS, "force": False}),
        ),
        (
            UiAction(action=UiActionType.HOVER, target=_target()),
            ("hover", (), {"timeout": DEFAULT_TIMEOUT_MS, "force": False}),
        ),
        (
            UiAction(action=UiActionType.FOCUS, target=_target()),
            ("focus", (), {"timeout": DEFAULT_TIMEOUT_MS}),
        ),
        (
            UiAction(action=UiActionType.SCROLL_INTO_VIEW, target=_target()),
            ("scroll", (), {"timeout": DEFAULT_TIMEOUT_MS}),
        ),
        (
            UiAction(
                action=UiActionType.WAIT_FOR,
                target=_target(),
                state="attached",
            ),
            ("wait", (), {"state": "attached", "timeout": DEFAULT_TIMEOUT_MS}),
        ),
    ],
)
def test_locator_actions_forward_bounded_arguments(
    action: UiAction,
    expected_call: tuple[str, tuple[object, ...], dict[str, object]],
) -> None:
    locator = FakeLocator()
    resolver = FakeResolver(locator)
    page = FakePage()

    outcome = asyncio.run(
        UiActionExecutor(cast("Any", resolver), DEFAULT_TIMEOUT_MS).execute(
            cast("Any", page), action
        )
    )

    assert outcome.page is page
    assert outcome.popup_opened is False
    assert resolver.calls == [(page, _target(), DEFAULT_TIMEOUT_MS)]
    assert locator.calls == [expected_call]


def test_click_without_popup_preserves_active_page() -> None:
    locator = FakeLocator()
    resolver = FakeResolver(locator)
    page = FakePage()
    action = UiAction(
        action=UiActionType.CLICK,
        target=_target(),
        timeout_ms=CUSTOM_TIMEOUT_MS,
        force=True,
    )

    outcome = asyncio.run(
        UiActionExecutor(cast("Any", resolver), DEFAULT_TIMEOUT_MS).execute(
            cast("Any", page), action
        )
    )

    assert outcome == ActionOutcome(cast("Any", page))
    assert locator.calls == [("click", (), {"timeout": CUSTOM_TIMEOUT_MS, "force": True})]


def test_click_popup_waits_for_new_page_load_and_returns_it() -> None:
    locator = FakeLocator()
    resolver = FakeResolver(locator)
    popup = FakePage()
    page = FakePage(popup=popup)
    action = UiAction(
        action=UiActionType.CLICK,
        target=_target(),
        expect_popup=True,
    )

    outcome = asyncio.run(
        UiActionExecutor(cast("Any", resolver), DEFAULT_TIMEOUT_MS).execute(
            cast("Any", page), action
        )
    )

    assert outcome == ActionOutcome(cast("Any", popup), popup_opened=True)
    assert page.calls == [("expect_popup", (), {"timeout": DEFAULT_TIMEOUT_MS})]
    assert locator.calls == [("click", (), {"timeout": DEFAULT_TIMEOUT_MS, "force": False})]
    assert popup.calls == [
        (
            "wait_for_load_state",
            ("domcontentloaded",),
            {"timeout": DEFAULT_TIMEOUT_MS},
        )
    ]


@pytest.mark.parametrize(
    ("choices", "expected_options"),
    [
        (
            [SelectChoice(value="available"), SelectChoice(value="pending")],
            {"value": ["available", "pending"], "timeout": DEFAULT_TIMEOUT_MS},
        ),
        (
            [SelectChoice(label="Available")],
            {"label": ["Available"], "timeout": DEFAULT_TIMEOUT_MS},
        ),
        (
            [SelectChoice(index=2)],
            {"index": [2], "timeout": DEFAULT_TIMEOUT_MS},
        ),
    ],
)
def test_select_uses_one_explicit_choice_strategy(
    choices: list[SelectChoice],
    expected_options: dict[str, object],
) -> None:
    locator = FakeLocator()
    resolver = FakeResolver(locator)
    action = UiAction(action=UiActionType.SELECT, target=_target(), choices=choices)

    asyncio.run(
        UiActionExecutor(cast("Any", resolver), DEFAULT_TIMEOUT_MS).execute(
            cast("Any", FakePage()), action
        )
    )

    assert locator.calls == [("select", (), expected_options)]


def test_select_rejects_mixed_choice_strategies() -> None:
    action = UiAction(
        action=UiActionType.SELECT,
        target=_target(),
        choices=[SelectChoice(value="available"), SelectChoice(label="Available")],
    )

    with pytest.raises(UiActionError, match="cannot mix"):
        asyncio.run(
            UiActionExecutor(cast("Any", FakeResolver(FakeLocator())), DEFAULT_TIMEOUT_MS).execute(
                cast("Any", FakePage()), action
            )
        )


def test_executor_defensively_rejects_missing_target_and_unsupported_action() -> None:
    executor = UiActionExecutor(
        cast("Any", FakeResolver(FakeLocator())),
        DEFAULT_TIMEOUT_MS,
    )
    missing_target = UiAction.model_construct(action=UiActionType.FILL, value="value")
    unsupported = UiAction.model_construct(
        action=cast("Any", "unsupported"),
        target=_target(),
    )

    with pytest.raises(UiActionError, match="requires a target"):
        asyncio.run(executor.execute(cast("Any", FakePage()), missing_target))
    with pytest.raises(UiActionError, match="Unsupported UI action"):
        asyncio.run(executor.execute(cast("Any", FakePage()), unsupported))


def test_executor_preserves_ui_errors_and_wraps_unexpected_failures() -> None:
    expected = UiActionError("safe action failure")
    action = UiAction(action=UiActionType.CLICK, target=_target())

    with pytest.raises(UiActionError) as preserved:
        asyncio.run(
            UiActionExecutor(
                cast("Any", FakeResolver(FakeLocator(), error=expected)),
                DEFAULT_TIMEOUT_MS,
            ).execute(cast("Any", FakePage()), action)
        )
    assert preserved.value is expected

    with pytest.raises(UiActionError, match="UI action 'click' failed") as wrapped:
        asyncio.run(
            UiActionExecutor(
                cast(
                    "Any",
                    FakeResolver(
                        FakeLocator(),
                        error=RuntimeError("synthetic internal detail"),
                    ),
                ),
                DEFAULT_TIMEOUT_MS,
            ).execute(cast("Any", FakePage()), action)
        )
    assert isinstance(wrapped.value.__cause__, RuntimeError)
    assert "synthetic internal detail" not in str(wrapped.value)
