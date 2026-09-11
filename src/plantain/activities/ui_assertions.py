"""Hard verification support for ``capturePageSnapshot.verify``."""

from __future__ import annotations

import re

from playwright.async_api import Locator, Page, expect

from plantain.activities.ui_errors import UiAssertionError
from plantain.activities.ui_locator import LocatorResolver
from plantain.models.ui import UiAssertion, UiAssertionType


class UiAssertionEngine:
    """Evaluates deterministic UI and page assertions with Playwright auto-waiting."""

    def __init__(self, locator_resolver: LocatorResolver, default_timeout_ms: int) -> None:
        self._locators = locator_resolver
        self._default_timeout_ms = default_timeout_ms

    async def verify(self, page: Page, assertion: UiAssertion) -> None:
        timeout_ms = assertion.timeout_ms or self._default_timeout_ms
        try:
            await self._verify(page, assertion, timeout_ms)
        except UiAssertionError:
            raise
        except Exception as exc:
            raise UiAssertionError(f"UI verification '{assertion.assertion.value}' failed") from exc

    async def _verify(
        self,
        page: Page,
        assertion: UiAssertion,
        timeout_ms: int,
    ) -> None:
        if await self._page_assertion(page, assertion, timeout_ms):
            return

        if assertion.target is None:
            raise UiAssertionError(f"Assertion '{assertion.assertion}' requires a target")

        if assertion.assertion in {
            UiAssertionType.HIDDEN,
            UiAssertionType.COUNT,
        }:
            resolved = await self._locators.build(page, assertion.target)
        else:
            resolved = await self._locators.resolve_one(
                page,
                assertion.target,
                timeout_ms=timeout_ms,
            )
        locator = resolved.locator

        if await self._state_assertion(locator, assertion, timeout_ms):
            return
        await self._content_assertion(locator, assertion, timeout_ms)

    @classmethod
    async def _page_assertion(
        cls,
        page: Page,
        assertion: UiAssertion,
        timeout_ms: int,
    ) -> bool:
        expected = cls._expected_value(assertion)
        if assertion.assertion is UiAssertionType.URL:
            await expect(page).to_have_url(expected, timeout=timeout_ms)
            return True
        if assertion.assertion is UiAssertionType.TITLE:
            await expect(page).to_have_title(expected, timeout=timeout_ms)
            return True
        return False

    @staticmethod
    async def _state_assertion(
        locator: Locator,
        assertion: UiAssertion,
        timeout_ms: int,
    ) -> bool:
        if assertion.assertion is UiAssertionType.VISIBLE:
            await expect(locator).to_be_visible(timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.HIDDEN:
            await expect(locator).to_be_hidden(timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.ENABLED:
            await expect(locator).to_be_enabled(timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.DISABLED:
            await expect(locator).to_be_disabled(timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.EDITABLE:
            await expect(locator).to_be_editable(timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.CHECKED:
            await expect(locator).to_be_checked(timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.UNCHECKED:
            await expect(locator).not_to_be_checked(timeout=timeout_ms)
        else:
            return False
        return True

    @staticmethod
    async def _content_assertion(
        locator: Locator,
        assertion: UiAssertion,
        timeout_ms: int,
    ) -> None:
        if assertion.assertion is UiAssertionType.TEXT:
            if assertion.contains is not None:
                await expect(locator).to_contain_text(assertion.contains, timeout=timeout_ms)
            else:
                await expect(locator).to_have_text(str(assertion.equals), timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.VALUE:
            if assertion.contains is not None:
                pattern = re.compile(re.escape(assertion.contains))
                await expect(locator).to_have_value(pattern, timeout=timeout_ms)
            else:
                await expect(locator).to_have_value(str(assertion.equals), timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.COUNT:
            expected_count = assertion.equals
            if not isinstance(expected_count, int) or isinstance(expected_count, bool):
                raise UiAssertionError("Count assertion requires an integer expectation")
            await expect(locator).to_have_count(expected_count, timeout=timeout_ms)
        elif assertion.assertion is UiAssertionType.ATTRIBUTE:
            await expect(locator).to_have_attribute(
                assertion.attribute or "",
                str(assertion.equals),
                timeout=timeout_ms,
            )
        else:
            raise UiAssertionError(f"Unsupported UI assertion '{assertion.assertion}'")

    @staticmethod
    def _expected_value(assertion: UiAssertion) -> str | re.Pattern[str]:
        if assertion.contains is not None:
            return re.compile(re.escape(assertion.contains))
        return str(assertion.equals)
