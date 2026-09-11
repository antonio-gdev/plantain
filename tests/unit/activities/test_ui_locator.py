"""Semantic locator construction, frame traversal, and disambiguation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from plantain.activities.ui_errors import LocatorResolutionError
from plantain.activities.ui_locator import LocatorResolver
from plantain.models.ui import FrameTarget, LocatorSpec

DEFAULT_TIMEOUT_MS = 1_000
EXPLICIT_TIMEOUT_MS = 2_000
SCAN_LIMIT = 3


class FakeLocator:
    def __init__(
        self,
        identity: str,
        *,
        count: int = 1,
        visible: bool = True,
        wait_error: Exception | None = None,
        nth_visibility: list[bool] | None = None,
        handle: Any = None,
    ) -> None:
        self.identity = identity
        self.count_value = count
        self.visible = visible
        self.wait_error = wait_error
        self.nth_visibility = nth_visibility or []
        self.handle = handle
        self.wait_calls: list[tuple[str, int]] = []
        self.nth_calls: list[int] = []
        self._children: dict[int, FakeLocator] = {}
        self.filter_calls: list[bool | None] = []
        self.filtered: FakeLocator | None = None

    @property
    def first(self) -> FakeLocator:
        return self

    async def wait_for(self, **options: object) -> None:
        state = cast("str", options["state"])
        timeout_ms = cast("int", options["timeout"])
        self.wait_calls.append((state, timeout_ms))
        if self.wait_error is not None:
            raise self.wait_error

    async def count(self) -> int:
        return self.count_value

    async def is_visible(self) -> bool:
        return self.visible

    def filter(self, *, visible: bool | None = None) -> FakeLocator:
        self.filter_calls.append(visible)
        if visible is not True:
            return self
        visible_count = sum(
            self.nth_visibility[index] if index < len(self.nth_visibility) else False
            for index in range(self.count_value)
        )
        self.filtered = FakeLocator(
            f"{self.identity}:visible",
            count=visible_count,
            visible=visible_count > 0,
        )
        return self.filtered

    def nth(self, index: int) -> FakeLocator:
        self.nth_calls.append(index)
        if index not in self._children:
            visible = self.nth_visibility[index] if index < len(self.nth_visibility) else False
            self._children[index] = FakeLocator(
                f"{self.identity}[{index}]",
                visible=visible,
            )
        return self._children[index]

    async def element_handle(self) -> Any:
        return self.handle


@dataclass
class FakeHandle:
    frame: FakeFrame | None

    async def content_frame(self) -> FakeFrame | None:
        return self.frame


@dataclass
class FakeFrame:
    name: str = ""
    url: str = ""
    child_frames: list[FakeFrame] = field(default_factory=list)
    locators: dict[str, FakeLocator] = field(default_factory=dict)
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def _result(self, identity: str) -> FakeLocator:
        return self.locators.setdefault(identity, FakeLocator(identity))

    def get_by_role(self, role: Any, *, name: str | None, exact: bool) -> FakeLocator:
        self.calls.append(("role", role, name, exact))
        return self._result(f"role:{role}:{name}")

    def get_by_label(self, value: str, *, exact: bool) -> FakeLocator:
        self.calls.append(("label", value, exact))
        return self._result(f"label:{value}")

    def get_by_placeholder(self, value: str, *, exact: bool) -> FakeLocator:
        self.calls.append(("placeholder", value, exact))
        return self._result(f"placeholder:{value}")

    def get_by_text(self, value: str, *, exact: bool) -> FakeLocator:
        self.calls.append(("text", value, exact))
        return self._result(f"text:{value}")

    def get_by_alt_text(self, value: str, *, exact: bool) -> FakeLocator:
        self.calls.append(("alt", value, exact))
        return self._result(f"alt:{value}")

    def get_by_title(self, value: str, *, exact: bool) -> FakeLocator:
        self.calls.append(("title", value, exact))
        return self._result(f"title:{value}")

    def get_by_test_id(self, value: str) -> FakeLocator:
        self.calls.append(("test_id", value))
        return self._result(f"test_id:{value}")

    def locator(self, value: str) -> FakeLocator:
        self.calls.append(("locator", value))
        return self._result(value)


@dataclass
class FakePage:
    main_frame: FakeFrame


@pytest.mark.parametrize(
    ("payload", "expected_call", "description"),
    [
        (
            {"role": "button", "name": "Continue"},
            ("role", "button", "Continue", True),
            "role 'button' with accessible name 'Continue'",
        ),
        ({"label": "First Name"}, ("label", "First Name", True), "label 'First Name'"),
        (
            {"placeholder": "Username"},
            ("placeholder", "Username", True),
            "placeholder 'Username'",
        ),
        ({"text": "Checkout"}, ("text", "Checkout", True), "text 'Checkout'"),
        ({"altText": "Cart"}, ("alt", "Cart", True), "alternative text 'Cart'"),
        ({"title": "Inventory"}, ("title", "Inventory", True), "title 'Inventory'"),
        ({"testId": "finish"}, ("test_id", "finish"), "test id 'finish'"),
        ({"css": "#finish"}, ("locator", "#finish"), "the supplied CSS locator"),
        (
            {"xpath": "//button"},
            ("locator", "xpath=//button"),
            "the supplied XPath locator",
        ),
    ],
)
def test_build_supports_every_locator_strategy(
    payload: dict[str, object],
    expected_call: tuple[object, ...],
    description: str,
) -> None:
    frame = FakeFrame()

    resolved = asyncio.run(
        LocatorResolver(DEFAULT_TIMEOUT_MS).build(
            cast("Any", FakePage(frame)),
            LocatorSpec.model_validate(payload),
        )
    )

    assert frame.calls == [expected_call]
    assert resolved.description == description


def test_build_applies_explicit_zero_based_index() -> None:
    frame = FakeFrame()
    base = FakeLocator(".item", count=3, nth_visibility=[True, True, True])
    frame.locators[".item"] = base

    resolved = asyncio.run(
        LocatorResolver(DEFAULT_TIMEOUT_MS).build(
            cast("Any", FakePage(frame)),
            LocatorSpec(css=".item", nth=2),
        )
    )

    assert resolved.locator is base._children[2]
    assert resolved.description.endswith("at zero-based index 2")


def test_build_rejects_defensive_empty_locator() -> None:
    with pytest.raises(LocatorResolutionError, match="supported strategy"):
        asyncio.run(
            LocatorResolver(DEFAULT_TIMEOUT_MS).build(
                cast("Any", FakePage(FakeFrame())),
                LocatorSpec.model_construct(),
            )
        )


def test_css_frame_traversal_resolves_nested_target() -> None:
    child = FakeFrame(name="checkout")
    parent = FakeFrame()
    parent.locators["iframe#checkout"] = FakeLocator(
        "iframe#checkout",
        handle=FakeHandle(child),
    )

    resolved = asyncio.run(
        LocatorResolver(DEFAULT_TIMEOUT_MS).build(
            cast("Any", FakePage(parent)),
            LocatorSpec(
                css="#continue",
                frames=[FrameTarget(css="iframe#checkout")],
            ),
        )
    )

    assert resolved.locator is child.locators["#continue"]


@pytest.mark.parametrize(
    ("frame_locator", "message"),
    [
        (FakeLocator("iframe", count=2), "matched 2 elements"),
        (FakeLocator("iframe", handle=None), "not an iframe"),
        (FakeLocator("iframe", handle=FakeHandle(None)), "not an iframe"),
    ],
)
def test_css_frame_traversal_rejects_ambiguous_or_non_frame_elements(
    frame_locator: FakeLocator,
    message: str,
) -> None:
    parent = FakeFrame(locators={"iframe": frame_locator})

    with pytest.raises(LocatorResolutionError, match=message):
        asyncio.run(
            LocatorResolver(DEFAULT_TIMEOUT_MS).build(
                cast("Any", FakePage(parent)),
                LocatorSpec(css="#target", frames=[FrameTarget(css="iframe")]),
            )
        )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (FrameTarget(name="checkout"), "named"),
        (FrameTarget(url="https://example.test/frame"), "url"),
    ],
)
def test_named_and_exact_url_frames_resolve_one_child(
    target: FrameTarget,
    expected: str,
) -> None:
    named = FakeFrame(name="checkout", url="https://example.test/named")
    url = FakeFrame(name="other", url="https://example.test/frame")
    parent = FakeFrame(child_frames=[named, url])

    resolved = asyncio.run(
        LocatorResolver(DEFAULT_TIMEOUT_MS).build(
            cast("Any", FakePage(parent)),
            LocatorSpec(css="#target", frames=[target]),
        )
    )

    expected_frame = named if expected == "named" else url
    assert resolved.locator is expected_frame.locators["#target"]


def test_named_frame_rejects_ambiguous_children() -> None:
    parent = FakeFrame(child_frames=[FakeFrame(name="duplicate"), FakeFrame(name="duplicate")])

    with pytest.raises(LocatorResolutionError, match="matched 2 child frames"):
        asyncio.run(
            LocatorResolver(DEFAULT_TIMEOUT_MS).build(
                cast("Any", FakePage(parent)),
                LocatorSpec(css="#target", frames=[FrameTarget(name="duplicate")]),
            )
        )


def test_resolve_one_uses_explicit_timeout_and_accepts_single_visible_match() -> None:
    base = FakeLocator("#target", visible=True)
    frame = FakeFrame(locators={"#target": base})

    resolved = asyncio.run(
        LocatorResolver(DEFAULT_TIMEOUT_MS).resolve_one(
            cast("Any", FakePage(frame)),
            LocatorSpec(css="#target"),
            timeout_ms=EXPLICIT_TIMEOUT_MS,
        )
    )

    assert resolved.locator is base
    assert base.wait_calls == [("attached", EXPLICIT_TIMEOUT_MS)]


def test_resolve_one_translates_attachment_timeout() -> None:
    base = FakeLocator("#target", wait_error=TimeoutError("synthetic detail"))
    frame = FakeFrame(locators={"#target": base})

    with pytest.raises(LocatorResolutionError, match="within 1000 ms") as raised:
        asyncio.run(
            LocatorResolver(DEFAULT_TIMEOUT_MS).resolve_one(
                cast("Any", FakePage(frame)),
                LocatorSpec(css="#target"),
            )
        )

    assert isinstance(raised.value.__cause__, TimeoutError)


def test_resolve_one_rejects_invisible_explicit_and_single_matches() -> None:
    for spec, base in (
        (
            LocatorSpec(css="#target", nth=0),
            FakeLocator("#target", nth_visibility=[False]),
        ),
        (LocatorSpec(css="#target"), FakeLocator("#target", visible=False)),
    ):
        frame = FakeFrame(locators={"#target": base})
        with pytest.raises(LocatorResolutionError, match="not visible"):
            asyncio.run(
                LocatorResolver(DEFAULT_TIMEOUT_MS).resolve_one(
                    cast("Any", FakePage(frame)),
                    spec,
                )
            )


def test_resolve_one_allows_invisible_mode_without_visibility_check() -> None:
    base = FakeLocator("#target", visible=False)
    frame = FakeFrame(locators={"#target": base})

    resolved = asyncio.run(
        LocatorResolver(DEFAULT_TIMEOUT_MS).resolve_one(
            cast("Any", FakePage(frame)),
            LocatorSpec(css="#target", visible=False),
        )
    )

    assert resolved.locator is base


def test_resolve_one_selects_the_only_visible_ambiguous_match() -> None:
    base = FakeLocator(
        ".item",
        count=3,
        nth_visibility=[False, True, False],
    )
    frame = FakeFrame(locators={".item": base})

    resolved = asyncio.run(
        LocatorResolver(DEFAULT_TIMEOUT_MS).resolve_one(
            cast("Any", FakePage(frame)),
            LocatorSpec(css=".item"),
        )
    )

    assert resolved.locator is base.filtered
    assert base.filter_calls == [True]


@pytest.mark.parametrize(
    ("visibility", "message"),
    [([False, False], "none visible"), ([True, True], "multiple visible")],
)
def test_resolve_one_rejects_zero_or_multiple_visible_matches(
    visibility: list[bool],
    message: str,
) -> None:
    base = FakeLocator(".item", count=2, nth_visibility=visibility)
    frame = FakeFrame(locators={".item": base})

    with pytest.raises(LocatorResolutionError, match=message):
        asyncio.run(
            LocatorResolver(DEFAULT_TIMEOUT_MS).resolve_one(
                cast("Any", FakePage(frame)),
                LocatorSpec(css=".item"),
            )
        )


def test_resolve_one_filters_all_matches_without_candidate_cutoff() -> None:
    base = FakeLocator(
        ".item",
        count=SCAN_LIMIT + 1,
        nth_visibility=[False] * SCAN_LIMIT + [True],
    )
    frame = FakeFrame(locators={".item": base})

    resolved = asyncio.run(
        LocatorResolver(DEFAULT_TIMEOUT_MS).resolve_one(
            cast("Any", FakePage(frame)),
            LocatorSpec(css=".item"),
        )
    )

    assert resolved.locator is base.filtered
    assert base.filter_calls == [True]
    assert base.nth_calls == []


def test_resolve_one_rejects_ambiguous_non_visible_locator() -> None:
    base = FakeLocator(".item", count=2)
    frame = FakeFrame(locators={".item": base})

    with pytest.raises(LocatorResolutionError, match="matched 2 elements"):
        asyncio.run(
            LocatorResolver(DEFAULT_TIMEOUT_MS).resolve_one(
                cast("Any", FakePage(frame)),
                LocatorSpec(css=".item", visible=False),
            )
        )
