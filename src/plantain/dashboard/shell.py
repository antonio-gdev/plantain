"""Reusable responsive shell for Plantain dashboard pages."""

from __future__ import annotations

from typing import Any, Literal, cast

import reflex as rx

from plantain.dashboard.state import DashboardState
from plantain.dashboard.theme import (
    APP_BACKGROUND,
    BORDER_COLOR,
    BRAND_BLUE,
    FOCUS_STYLE,
    MUTED_TEXT_COLOR,
    PAGE_GUTTER,
    PAGE_VERTICAL_PADDING,
    PANEL_BACKGROUND,
    TEXT_COLOR,
    TOPBAR_BACKGROUND,
)


def component(value: Any) -> rx.Component:
    """Restore the component type omitted by Reflex's dynamic factory stubs."""

    return cast("rx.Component", value)


def action_link(
    label: str,
    *,
    icon: str,
    href: str,
    variant: Literal["solid", "soft"] = "solid",
) -> rx.Component:
    """Render one accessible anchor with button-like visual treatment."""

    solid = variant == "solid"
    return component(
        rx.link(
            rx.hstack(
                rx.icon(icon, size=15),
                rx.text(label),
                align="center",
                spacing="2",
            ),
            aria_label=label,
            background=BRAND_BLUE if solid else "var(--blue-3)",
            border=(f"1px solid {BRAND_BLUE}" if solid else "1px solid var(--blue-6)"),
            border_radius="8px",
            color="white" if solid else "var(--blue-11)",
            font_size="0.8rem",
            font_weight="650",
            href=href,
            min_height="36px",
            padding="0.5rem 0.75rem",
            text_decoration="none",
            white_space="nowrap",
            width="fit-content",
            _hover={
                "background": "var(--blue-10)" if solid else "var(--blue-4)",
            },
            **FOCUS_STYLE,
        )
    )


def _skip_link() -> rx.Component:
    return component(
        rx.link(
            "Skip to main content",
            href="#main-content",
            background=BRAND_BLUE,
            border_radius="8px",
            color="white",
            font_weight="650",
            left="1rem",
            padding="0.6rem 0.8rem",
            position="fixed",
            top="-4rem",
            z_index="1000",
            _focus={"top": "1rem"},
            **FOCUS_STYLE,
        )
    )


def _brand(*, compact: bool = False) -> rx.Component:
    return component(
        rx.image(
            src=("/plantain_favicon.png" if compact else "/plantain_logo.png"),
            alt="Plantain",
            width="24px" if compact else "104px",
            height="auto",
        )
    )


def _navigation_item(
    label: str,
    *,
    icon: str,
    href: str,
    active: bool,
    close_drawer: bool = False,
    compact: bool = False,
) -> rx.Component:
    item = component(
        rx.link(
            rx.hstack(
                rx.icon(icon, size=18),
                *(() if compact else (rx.text(label, font_weight="650"),)),
                align="center",
                border_radius="10px",
                color="var(--blue-12)" if active else TEXT_COLOR,
                background="var(--blue-4)" if active else "transparent",
                justify="center" if compact else "start",
                min_height="42px",
                padding="0.65rem" if compact else "0.65rem 0.75rem",
                spacing="3",
                width="100%",
                **FOCUS_STYLE,
            ),
            aria_label=label,
            aria_current="page" if active else None,
            href=href,
            text_decoration="none",
            title=label if compact else None,
            width="100%",
        )
    )
    if close_drawer:
        return component(rx.drawer.close(item, as_child=True))
    return item


def _navigation(
    active: str,
    *,
    close_drawer: bool = False,
    compact: bool = False,
) -> rx.Component:
    return component(
        rx.vstack(
            *(
                ()
                if compact
                else (
                    rx.text(
                        "WORKSPACE",
                        color=MUTED_TEXT_COLOR,
                        font_size="0.68rem",
                        font_weight="700",
                        letter_spacing="0.12em",
                        padding_left="0.7rem",
                    ),
                )
            ),
            _navigation_item(
                "Overview",
                icon="layout-dashboard",
                href="/",
                active=active == "overview",
                close_drawer=close_drawer,
                compact=compact,
            ),
            _navigation_item(
                "Create",
                icon="plus",
                href="/create",
                active=active == "create",
                close_drawer=close_drawer,
                compact=compact,
            ),
            _navigation_item(
                "Tests",
                icon="list-checks",
                href="/tests",
                active=active == "tests",
                close_drawer=close_drawer,
                compact=compact,
            ),
            _navigation_item(
                "Runs and Results",
                icon="history",
                href="/runs",
                active=active == "runs",
                close_drawer=close_drawer,
                compact=compact,
            ),
            *(
                ()
                if compact
                else (
                    rx.text(
                        "PREFERENCES",
                        color=MUTED_TEXT_COLOR,
                        font_size="0.68rem",
                        font_weight="700",
                        letter_spacing="0.12em",
                        margin_top="0.65rem",
                        padding_left="0.7rem",
                    ),
                )
            ),
            _navigation_item(
                "Settings",
                icon="settings",
                href="/settings",
                active=active == "settings",
                close_drawer=close_drawer,
                compact=compact,
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


def _sidebar_header(*, compact: bool) -> rx.Component:
    toggle = rx.icon_button(
        rx.icon("chevron-right" if compact else "chevron-left", size=18),
        aria_label=("Expand primary navigation" if compact else "Collapse primary navigation"),
        color_scheme="gray",
        on_click=DashboardState.toggle_navigation,
        size="2",
        title=("Expand navigation" if compact else "Collapse navigation"),
        variant="ghost",
        **FOCUS_STYLE,
    )
    if compact:
        return component(
            rx.center(
                toggle,
                width="100%",
            )
        )
    return component(
        rx.hstack(
            rx.spacer(),
            toggle,
            align="center",
            width="100%",
        )
    )


def _sidebar(active: str, *, compact: bool = False) -> rx.Component:
    width = "72px" if compact else "248px"
    return component(
        rx.box(
            rx.vstack(
                _sidebar_header(compact=compact),
                rx.el.nav(
                    _navigation(active, compact=compact),
                    aria_label="Primary navigation",
                    width="100%",
                ),
                background=PANEL_BACKGROUND,
                height="100vh",
                overflow_y="auto",
                padding="0.7rem" if compact else "1.2rem",
                position="sticky",
                spacing="5",
                top="0",
                width="100%",
            ),
            background=PANEL_BACKGROUND,
            border_right=f"1px solid {BORDER_COLOR}",
            flex_shrink="0",
            min_height="100vh",
            width=width,
        )
    )


def _mobile_navigation(active: str) -> rx.Component:
    return component(
        rx.drawer.root(
            rx.drawer.trigger(
                rx.icon_button(
                    rx.icon("menu", size=19),
                    aria_label="Open primary navigation",
                    color_scheme="blue",
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                as_child=True,
            ),
            rx.drawer.portal(
                rx.drawer.overlay(
                    background="rgba(15, 23, 42, 0.46)",
                    inset="0",
                    position="fixed",
                    z_index="50",
                ),
                rx.drawer.content(
                    rx.vstack(
                        rx.hstack(
                            rx.drawer.title(
                                "Navigation",
                                font_size="1rem",
                                font_weight="750",
                            ),
                            rx.spacer(),
                            rx.drawer.close(
                                rx.icon_button(
                                    rx.icon("x", size=18),
                                    aria_label="Close primary navigation",
                                    color_scheme="gray",
                                    size="2",
                                    variant="ghost",
                                    **FOCUS_STYLE,
                                ),
                                as_child=True,
                            ),
                            align="center",
                            width="100%",
                        ),
                        rx.el.nav(
                            _navigation(active, close_drawer=True),
                            aria_label="Primary navigation",
                            margin_top="0.5rem",
                            width="100%",
                        ),
                        align="stretch",
                        box_sizing="border-box",
                        height="100%",
                        overflow_x="hidden",
                        padding="1.2rem",
                        spacing="4",
                        width="100%",
                    ),
                    background=PANEL_BACKGROUND,
                    bottom="0",
                    box_sizing="border-box",
                    left="0",
                    overflow_x="hidden",
                    overflow_y="auto",
                    position="fixed",
                    right="0",
                    top="0",
                    width="100vw",
                    z_index="51",
                ),
            ),
            direction="right",
        )
    )


def _topbar(title: str, active: str) -> rx.Component:
    return component(
        rx.hstack(
            component(rx.desktop_only(_brand())),
            component(rx.mobile_and_tablet(_brand(compact=True))),
            rx.spacer(),
            component(
                rx.tablet_and_desktop(
                    rx.badge(
                        rx.icon("shield-check", size=14),
                        "Local workspace",
                        color_scheme="blue",
                        variant="soft",
                    )
                )
            ),
            rx.color_mode.button(position="relative"),
            component(rx.mobile_and_tablet(_mobile_navigation(active))),
            align="center",
            aria_label=title + " workspace header",
            background=TOPBAR_BACKGROUND,
            border_bottom=f"1px solid {BORDER_COLOR}",
            min_height="64px",
            padding_left=PAGE_GUTTER,
            padding_right=PAGE_GUTTER,
            width="100%",
        )
    )


def page_shell(
    content: rx.Component,
    *,
    active: str,
    title: str,
) -> rx.Component:
    """Place one real page inside the shared responsive application shell."""

    return component(
        rx.flex(
            _skip_link(),
            component(
                rx.desktop_only(
                    rx.cond(
                        DashboardState.navigation_collapsed,
                        _sidebar(active, compact=True),
                        _sidebar(active),
                    )
                )
            ),
            rx.vstack(
                _topbar(title, active),
                rx.el.main(
                    rx.flex(
                        content,
                        align="start",
                        box_sizing="border-box",
                        justify="center",
                        padding_bottom=PAGE_VERTICAL_PADDING,
                        padding_left=PAGE_GUTTER,
                        padding_right=PAGE_GUTTER,
                        padding_top=PAGE_VERTICAL_PADDING,
                        width="100%",
                    ),
                    id="main-content",
                    tab_index=-1,
                    width="100%",
                ),
                align="stretch",
                flex="1",
                min_width="0",
                spacing="0",
            ),
            align="stretch",
            background=APP_BACKGROUND,
            min_height="100vh",
            width="100%",
        )
    )


__all__ = ["action_link", "component", "page_shell"]
