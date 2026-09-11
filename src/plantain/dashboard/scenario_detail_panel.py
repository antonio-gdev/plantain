"""Focused drawer for inspecting bounded, sanitized scenario steps."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.scenario_state import ScenarioCatalogState
from plantain.dashboard.shell import component
from plantain.dashboard.theme import (
    BORDER_COLOR,
    BRAND_BLUE,
    FOCUS_STYLE,
    MONO_FONT_FAMILY,
    MUTED_TEXT_COLOR,
    PANEL_BACKGROUND,
    PANEL_PADDING,
    SUBTLE_BACKGROUND,
)


def _drawer_header() -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.hstack(
                    rx.icon("file-code-2", size=18, color=BRAND_BLUE),
                    rx.drawer.title(
                        rx.cond(
                            ScenarioCatalogState.test_detail_name != "",
                            ScenarioCatalogState.test_detail_name,
                            "Test steps",
                        ),
                        font_size="1rem",
                        font_weight="750",
                    ),
                    rx.badge(
                        ScenarioCatalogState.test_detail_total_steps,
                        " steps",
                        color_scheme="blue",
                        variant="soft",
                    ),
                    align="center",
                    spacing="2",
                ),
                rx.spacer(),
                rx.drawer.close(
                    rx.icon_button(
                        rx.icon("x", size=18),
                        aria_label="Close test steps",
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
            rx.drawer.description(
                ScenarioCatalogState.test_detail_source,
                color=MUTED_TEXT_COLOR,
                font_family=MONO_FONT_FAMILY,
                font_size="0.75rem",
            ),
            align="stretch",
            spacing="1",
            width="100%",
        )
    )


def _step_preview(item: Any) -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.badge(
                    "Step ",
                    item["position"],
                    color_scheme="blue",
                    variant="soft",
                ),
                rx.text(item["activity"], font_size="0.85rem", font_weight="700"),
                rx.cond(
                    item["step_id"] != "",
                    rx.badge(
                        "ID · ",
                        item["step_id"],
                        color_scheme="gray",
                        variant="soft",
                    ),
                ),
                align="center",
                gap="2",
                wrap="wrap",
                width="100%",
            ),
            rx.el.pre(
                item["yaml"],
                background="var(--gray-1)",
                border_radius="10px",
                color="var(--gray-12)",
                font_family=MONO_FONT_FAMILY,
                font_size="0.76rem",
                line_height="1.55",
                margin="0",
                overflow_x="auto",
                padding="0.9rem",
                white_space="pre-wrap",
                width="100%",
            ),
            rx.cond(
                item["limited"] == "true",
                rx.text(
                    "This step preview reached its explicit safety limit.",
                    color="var(--yellow-11)",
                    font_size="0.72rem",
                    role="status",
                ),
            ),
            align="stretch",
            background=SUBTLE_BACKGROUND,
            border_radius="12px",
            padding="0.9rem",
            spacing="3",
            width="100%",
        )
    )


def _detail_pagination() -> rx.Component:
    return component(
        rx.cond(
            ScenarioCatalogState.test_detail_page_count > 1,
            rx.hstack(
                rx.button(
                    rx.icon("chevron-left", size=15),
                    "Previous",
                    aria_label="Previous test-step page",
                    disabled=(
                        ~ScenarioCatalogState.test_detail_has_previous
                        | ScenarioCatalogState.test_detail_loading
                    ),
                    on_click=cast(
                        "Any",
                        ScenarioCatalogState.show_test_detail_page,
                    )(ScenarioCatalogState.test_detail_page - 1),
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                rx.spacer(),
                rx.text(
                    "Page ",
                    ScenarioCatalogState.test_detail_page,
                    " of ",
                    ScenarioCatalogState.test_detail_page_count,
                    color=MUTED_TEXT_COLOR,
                    font_size="0.76rem",
                ),
                rx.spacer(),
                rx.button(
                    "Next",
                    rx.icon("chevron-right", size=15),
                    aria_label="Next test-step page",
                    disabled=(
                        ~ScenarioCatalogState.test_detail_has_next
                        | ScenarioCatalogState.test_detail_loading
                    ),
                    on_click=cast(
                        "Any",
                        ScenarioCatalogState.show_test_detail_page,
                    )(ScenarioCatalogState.test_detail_page + 1),
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                align="center",
                width="100%",
            ),
        )
    )


def _drawer_body() -> rx.Component:
    return component(
        rx.box(
            rx.cond(
                ScenarioCatalogState.test_detail_loading,
                rx.center(
                    rx.vstack(
                        rx.spinner(size="3"),
                        rx.text("Loading steps…", color=MUTED_TEXT_COLOR),
                        align="center",
                        spacing="2",
                    ),
                    min_height="240px",
                    role="status",
                    width="100%",
                ),
                rx.cond(
                    ScenarioCatalogState.test_detail_error != "",
                    rx.callout(
                        ScenarioCatalogState.test_detail_error,
                        color_scheme="red",
                        icon="circle-alert",
                        role="alert",
                        width="100%",
                    ),
                    rx.vstack(
                        rx.foreach(
                            ScenarioCatalogState.test_detail_steps,
                            _step_preview,
                        ),
                        rx.text(
                            ScenarioCatalogState.test_detail_notice,
                            color=MUTED_TEXT_COLOR,
                            font_size="0.74rem",
                            role="status",
                        ),
                        _detail_pagination(),
                        align="stretch",
                        spacing="3",
                        width="100%",
                    ),
                ),
            ),
            flex="1",
            overflow_y="auto",
            padding_right="0.2rem",
            width="100%",
        )
    )


def scenario_detail_drawer() -> rx.Component:
    """Render one controlled, dismissible test-detail drawer."""

    return component(
        rx.drawer.root(
            rx.drawer.portal(
                rx.drawer.overlay(
                    background="rgba(15, 23, 42, 0.46)",
                    inset="0",
                    position="fixed",
                    z_index="60",
                ),
                rx.drawer.content(
                    rx.vstack(
                        _drawer_header(),
                        _drawer_body(),
                        align="stretch",
                        height="100%",
                        padding=PANEL_PADDING,
                        spacing="4",
                        width="100%",
                    ),
                    background=PANEL_BACKGROUND,
                    border_left=f"1px solid {BORDER_COLOR}",
                    bottom="0",
                    box_shadow="-12px 0 32px rgba(15, 23, 42, 0.18)",
                    left="auto",
                    max_width="680px",
                    overflow="hidden",
                    position="fixed",
                    right="0",
                    top="0",
                    width="96vw",
                    z_index="61",
                ),
            ),
            direction="right",
            on_open_change=ScenarioCatalogState.change_test_detail_open,
            open=ScenarioCatalogState.test_detail_open,
        )
    )


__all__ = ["scenario_detail_drawer"]
