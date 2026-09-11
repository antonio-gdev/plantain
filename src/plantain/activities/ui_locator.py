"""Semantic-first Playwright locator resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from playwright.async_api import Frame, Locator, Page

from plantain.activities.ui_errors import LocatorResolutionError
from plantain.models.ui import FrameTarget, LocatorSpec


@dataclass(frozen=True, slots=True)
class ResolvedLocator:
    """A Playwright locator paired with a secret-free diagnostic description."""

    locator: Locator
    description: str


class LocatorResolver:
    """Build and validate deterministic locators from YAML locator records."""

    def __init__(self, default_timeout_ms: int) -> None:
        self._default_timeout_ms = default_timeout_ms

    async def build(self, page: Page, spec: LocatorSpec) -> ResolvedLocator:
        """Build a locator without imposing singularity.

        This path is used for count and absence assertions, where zero or many
        matches are meaningful rather than resolution failures.
        """

        frame = await self._resolve_frame(page, spec.frames)
        locator, description = self._build_in_frame(frame, spec)
        if spec.nth is not None:
            locator = locator.nth(spec.nth)
            description = f"{description} at zero-based index {spec.nth}"
        return ResolvedLocator(locator=locator, description=description)

    async def resolve_one(
        self,
        page: Page,
        spec: LocatorSpec,
        *,
        timeout_ms: int | None = None,
    ) -> ResolvedLocator:
        """Resolve exactly one usable element and reject ambiguous matches."""

        resolved = await self.build(page, spec)
        locator = resolved.locator
        timeout = timeout_ms or self._default_timeout_ms

        try:
            await locator.first.wait_for(state="attached", timeout=timeout)
        except Exception as exc:  # Playwright exposes several timeout subclasses.
            raise LocatorResolutionError(
                f"No element matched {resolved.description} within {timeout} ms"
            ) from exc

        if spec.nth is not None:
            if spec.visible and not await locator.is_visible():
                raise LocatorResolutionError(
                    f"Element matched {resolved.description}, but it is not visible"
                )
            return resolved

        count = await locator.count()
        if count == 1:
            if spec.visible and not await locator.is_visible():
                raise LocatorResolutionError(
                    f"Element matched {resolved.description}, but it is not visible"
                )
            return resolved

        if spec.visible:
            visible_locator = locator.filter(visible=True)
            visible_count = await visible_locator.count()
            if visible_count == 1:
                return ResolvedLocator(
                    locator=visible_locator,
                    description=resolved.description,
                )
            visibility = "none" if visible_count == 0 else "multiple"
            raise LocatorResolutionError(
                f"Locator {resolved.description} matched {count} elements "
                f"({visibility} visible); make the semantic locator more specific "
                "or set nth explicitly"
            )

        raise LocatorResolutionError(
            f"Locator {resolved.description} matched {count} elements; "
            "make it more specific or set nth explicitly"
        )

    async def _resolve_frame(self, page: Page, targets: list[FrameTarget]) -> Frame:
        current = page.main_frame
        for target in targets:
            current = await self._resolve_child_frame(current, target)
        return current

    async def _resolve_child_frame(self, parent: Frame, target: FrameTarget) -> Frame:
        if target.css is not None:
            frame_elements = parent.locator(target.css)
            count = await frame_elements.count()
            if count != 1:
                raise LocatorResolutionError(
                    f"Frame CSS locator matched {count} elements; exactly one is required"
                )
            handle = await frame_elements.element_handle()
            child = await handle.content_frame() if handle is not None else None
            if child is None:
                raise LocatorResolutionError("The matched CSS element is not an iframe")
            return child

        candidates = parent.child_frames
        if target.name is not None:
            matches = [frame for frame in candidates if frame.name == target.name]
            description = f"name '{target.name}'"
        else:
            matches = [frame for frame in candidates if frame.url == cast(str, target.url)]
            description = "the requested exact URL"
        if len(matches) != 1:
            raise LocatorResolutionError(
                f"Frame {description} matched {len(matches)} child frames; exactly one is required"
            )
        return matches[0]

    @staticmethod
    def _build_in_frame(frame: Frame, spec: LocatorSpec) -> tuple[Locator, str]:
        if spec.role is not None:
            locator = frame.get_by_role(
                cast(Any, spec.role),
                name=spec.name,
                exact=spec.exact,
            )
            description = f"role '{spec.role}'"
            if spec.name is not None:
                description += f" with accessible name '{spec.name}'"
            return locator, description
        semantic = LocatorResolver._semantic_text_locator(frame, spec)
        if semantic is not None:
            return semantic
        if spec.test_id is not None:
            return frame.get_by_test_id(spec.test_id), f"test id '{spec.test_id}'"
        if spec.css is not None:
            return frame.locator(spec.css), "the supplied CSS locator"
        if spec.xpath is not None:
            return frame.locator(f"xpath={spec.xpath}"), "the supplied XPath locator"
        raise LocatorResolutionError("Locator does not contain a supported strategy")

    @staticmethod
    def _semantic_text_locator(frame: Frame, spec: LocatorSpec) -> tuple[Locator, str] | None:
        if spec.label is not None:
            return frame.get_by_label(spec.label, exact=spec.exact), f"label '{spec.label}'"
        if spec.placeholder is not None:
            return (
                frame.get_by_placeholder(spec.placeholder, exact=spec.exact),
                f"placeholder '{spec.placeholder}'",
            )
        if spec.text is not None:
            return frame.get_by_text(spec.text, exact=spec.exact), f"text '{spec.text}'"
        if spec.alt_text is not None:
            return (
                frame.get_by_alt_text(spec.alt_text, exact=spec.exact),
                f"alternative text '{spec.alt_text}'",
            )
        if spec.title is not None:
            return frame.get_by_title(spec.title, exact=spec.exact), f"title '{spec.title}'"
        return None


__all__ = ["LocatorResolver", "ResolvedLocator"]
