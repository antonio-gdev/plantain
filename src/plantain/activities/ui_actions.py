"""Execution engine for generic declarative browser actions."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Locator, Page

from plantain.activities.ui_errors import UiActionError
from plantain.activities.ui_locator import LocatorResolver
from plantain.models.ui import SelectChoice, UiAction, UiActionType


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """Result of one action, including a newly active popup when requested."""

    page: Page
    popup_opened: bool = False


class UiActionExecutor:
    """Executes only the bounded action vocabulary accepted by the YAML schema."""

    def __init__(self, locator_resolver: LocatorResolver, default_timeout_ms: int) -> None:
        self._locators = locator_resolver
        self._default_timeout_ms = default_timeout_ms

    async def execute(self, page: Page, action: UiAction) -> ActionOutcome:
        timeout_ms = action.timeout_ms or self._default_timeout_ms
        try:
            return await self._execute(page, action, timeout_ms)
        except UiActionError:
            raise
        except Exception as exc:
            raise UiActionError(f"UI action '{action.action.value}' failed") from exc

    async def _execute(
        self,
        page: Page,
        action: UiAction,
        timeout_ms: int,
    ) -> ActionOutcome:
        page_outcome = await self._page_action(page, action, timeout_ms)
        if page_outcome is not None:
            return page_outcome

        if action.target is None:
            raise UiActionError(f"Action '{action.action}' requires a target")
        resolved = await self._locators.resolve_one(
            page,
            action.target,
            timeout_ms=timeout_ms,
        )
        if action.action is UiActionType.CLICK:
            return await self._click(page, resolved.locator, action, timeout_ms)
        await self._locator_action(resolved.locator, action, timeout_ms)
        return ActionOutcome(page)

    @staticmethod
    async def _page_action(
        page: Page,
        action: UiAction,
        timeout_ms: int,
    ) -> ActionOutcome | None:
        if action.action is UiActionType.WAIT_FOR_URL:
            await page.wait_for_url(action.value or "", timeout=timeout_ms)
            return ActionOutcome(page)
        if action.action is UiActionType.RELOAD:
            await page.reload(timeout=timeout_ms, wait_until="domcontentloaded")
            return ActionOutcome(page)
        if action.action is UiActionType.GO_BACK:
            await page.go_back(timeout=timeout_ms, wait_until="domcontentloaded")
            return ActionOutcome(page)
        if action.action is UiActionType.GO_FORWARD:
            await page.go_forward(timeout=timeout_ms, wait_until="domcontentloaded")
            return ActionOutcome(page)
        return None

    @staticmethod
    async def _click(
        page: Page,
        locator: Locator,
        action: UiAction,
        timeout_ms: int,
    ) -> ActionOutcome:
        if not action.expect_popup:
            await locator.click(timeout=timeout_ms, force=action.force)
            return ActionOutcome(page)
        async with page.expect_popup(timeout=timeout_ms) as popup_info:
            await locator.click(timeout=timeout_ms, force=action.force)
        popup = await popup_info.value
        await popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        return ActionOutcome(popup, popup_opened=True)

    async def _locator_action(
        self,
        locator: Locator,
        action: UiAction,
        timeout_ms: int,
    ) -> None:
        if action.action is UiActionType.FILL:
            await locator.fill(action.value or "", timeout=timeout_ms, force=action.force)
        elif action.action is UiActionType.CLEAR:
            await locator.clear(timeout=timeout_ms, force=action.force)
        elif action.action is UiActionType.TYPE:
            await locator.press_sequentially(
                action.value or "",
                delay=action.delay_ms,
                timeout=timeout_ms,
            )
        elif action.action is UiActionType.PRESS:
            await locator.press(action.key or action.value or "", timeout=timeout_ms)
        elif action.action is UiActionType.SELECT:
            await self._select(locator, action.choices, timeout_ms)
        elif action.action is UiActionType.CHECK:
            await locator.check(timeout=timeout_ms, force=action.force)
        elif action.action is UiActionType.UNCHECK:
            await locator.uncheck(timeout=timeout_ms, force=action.force)
        elif action.action is UiActionType.HOVER:
            await locator.hover(timeout=timeout_ms, force=action.force)
        elif action.action is UiActionType.FOCUS:
            await locator.focus(timeout=timeout_ms)
        elif action.action is UiActionType.SCROLL_INTO_VIEW:
            await locator.scroll_into_view_if_needed(timeout=timeout_ms)
        elif action.action is UiActionType.WAIT_FOR:
            await locator.wait_for(state=action.state, timeout=timeout_ms)
        else:
            raise UiActionError(f"Unsupported UI action '{action.action}'")

    @staticmethod
    async def _select(
        locator: Locator,
        choices: list[SelectChoice],
        timeout_ms: int,
    ) -> None:
        value_choices = [choice.value for choice in choices if choice.value is not None]
        label_choices = [choice.label for choice in choices if choice.label is not None]
        index_choices = [choice.index for choice in choices if choice.index is not None]
        populated = sum(bool(group) for group in (value_choices, label_choices, index_choices))
        if populated != 1:
            raise UiActionError(
                "One select action cannot mix value, label, and index choice strategies"
            )
        if value_choices:
            await locator.select_option(value=value_choices, timeout=timeout_ms)
        elif label_choices:
            await locator.select_option(label=label_choices, timeout=timeout_ms)
        else:
            await locator.select_option(index=index_choices, timeout=timeout_ms)
