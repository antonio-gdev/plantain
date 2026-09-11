"""Optional local evidence attachment surface for test creation."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.shell import component
from plantain.dashboard.state import DashboardState
from plantain.dashboard.theme import (
    BORDER_COLOR,
    BRAND_BLUE,
    FOCUS_STYLE,
    PANEL_BACKGROUND,
    PANEL_PADDING,
    SUBTLE_BACKGROUND,
)

_SOURCE_TYPES = [
    "Application project",
    "Requirements",
    "API contract",
]


def _status_badge(source: Any) -> rx.Component:
    return component(
        rx.cond(
            source["status"] == "Unavailable",
            rx.badge(
                "Unavailable",
                color_scheme="red",
                variant="soft",
            ),
            rx.cond(
                source["status"] == "Partial",
                rx.badge(
                    "Partial",
                    color_scheme="yellow",
                    variant="soft",
                ),
                rx.badge(
                    "Ready",
                    color_scheme="blue",
                    variant="soft",
                ),
            ),
        )
    )


def _source_row(source: Any) -> rx.Component:
    return component(
        rx.hstack(
            rx.box(
                rx.icon("file-search", size=17, color=BRAND_BLUE),
                background="var(--blue-3)",
                border_radius="9px",
                padding="0.55rem",
                flex_shrink="0",
            ),
            rx.vstack(
                rx.hstack(
                    rx.text(
                        source["label"],
                        font_size="0.82rem",
                        font_weight="700",
                    ),
                    _status_badge(source),
                    align="center",
                    wrap="wrap",
                ),
                rx.text(
                    source["kind"],
                    " · ",
                    source["files"],
                    color="var(--gray-9)",
                    font_size="0.72rem",
                ),
                align="start",
                min_width="0",
                spacing="1",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("x", size=15),
                aria_label="Remove context source",
                color_scheme="gray",
                disabled=DashboardState.context_busy,
                on_click=cast("Any", DashboardState.detach_context)(source["source_id"]),
                size="1",
                variant="ghost",
                **FOCUS_STYLE,
            ),
            align="center",
            background=SUBTLE_BACKGROUND,
            border_radius="10px",
            padding="0.75rem",
            spacing="3",
            width="100%",
        )
    )


def _source_list() -> rx.Component:
    return component(
        rx.cond(
            DashboardState.context_source_count > 0,
            rx.vstack(
                rx.foreach(DashboardState.context_sources, _source_row),
                align="stretch",
                spacing="2",
                width="100%",
            ),
            rx.hstack(
                rx.icon("folder-plus", size=17, color="var(--gray-8)"),
                rx.text(
                    "No local context attached. This is optional.",
                    color="var(--gray-9)",
                    font_size="0.78rem",
                ),
                align="center",
                background=SUBTLE_BACKGROUND,
                border_radius="10px",
                padding="0.8rem",
                width="100%",
            ),
        )
    )


def _source_pagination() -> rx.Component:
    return component(
        rx.cond(
            DashboardState.context_total_pages > 1,
            rx.hstack(
                rx.button(
                    rx.icon("chevron-left", size=15),
                    aria_label="Previous context page",
                    disabled=DashboardState.context_busy | (DashboardState.context_page <= 1),
                    on_click=cast("Any", DashboardState.change_context_page)(-1),
                    size="1",
                    variant="ghost",
                    **FOCUS_STYLE,
                ),
                rx.spacer(),
                rx.text(
                    "Page ",
                    DashboardState.context_page,
                    " of ",
                    DashboardState.context_total_pages,
                    color="var(--gray-9)",
                    font_size="0.72rem",
                    role="status",
                ),
                rx.spacer(),
                rx.button(
                    rx.icon("chevron-right", size=15),
                    aria_label="Next context page",
                    disabled=DashboardState.context_busy
                    | (DashboardState.context_page >= DashboardState.context_total_pages),
                    on_click=cast("Any", DashboardState.change_context_page)(1),
                    size="1",
                    variant="ghost",
                    **FOCUS_STYLE,
                ),
                align="center",
                width="100%",
            ),
        )
    )


def _attachment_form() -> rx.Component:
    return component(
        rx.form.root(
            rx.flex(
                rx.select(
                    _SOURCE_TYPES,
                    aria_label="Context source type",
                    name="context_kind",
                    default_value="Application project",
                    required=True,
                    width={"initial": "100%", "md": "210px"},
                ),
                rx.input(
                    name="context_path",
                    placeholder="Local project folder or source file",
                    aria_label="Local context source path",
                    max_length=4_096,
                    required=True,
                    width="100%",
                    **FOCUS_STYLE,
                ),
                rx.button(
                    rx.icon("plus", size=15),
                    "Add context",
                    type="submit",
                    color_scheme="blue",
                    loading=DashboardState.context_busy,
                    flex_shrink="0",
                    **FOCUS_STYLE,
                ),
                align="stretch",
                direction={"initial": "column", "md": "row"},
                gap="2",
                width="100%",
            ),
            on_submit=DashboardState.attach_context,
            reset_on_submit=True,
            width="100%",
        )
    )


def context_panel() -> rx.Component:
    """Render explicit, optional local context management."""

    return component(
        rx.vstack(
            _attachment_form(),
            rx.callout(
                DashboardState.agent_context_destination,
                icon="shield-check",
                color_scheme="blue",
                role="status",
                size="1",
                width="100%",
            ),
            rx.cond(
                DashboardState.context_error_message != "",
                rx.callout(
                    DashboardState.context_error_message,
                    icon="triangle-alert",
                    color_scheme="red",
                    role="alert",
                    size="1",
                    width="100%",
                ),
            ),
            rx.cond(
                DashboardState.context_notice != "",
                rx.callout(
                    DashboardState.context_notice,
                    icon="info",
                    color_scheme="yellow",
                    role="status",
                    size="1",
                    width="100%",
                ),
            ),
            _source_list(),
            _source_pagination(),
            rx.text(
                "Supported files are indexed locally; only bounded, relevant, "
                "redacted excerpts can enter an agent request.",
                color="var(--gray-9)",
                font_size="0.7rem",
            ),
            align="stretch",
            spacing="4",
            width="100%",
        )
    )


def context_drawer() -> rx.Component:
    """Keep optional source management beside the request without consuming the page."""

    return component(
        rx.drawer.root(
            rx.drawer.trigger(
                rx.button(
                    rx.icon("folder-plus", size=15),
                    "Add context",
                    rx.cond(
                        DashboardState.context_source_count > 0,
                        rx.badge(
                            DashboardState.context_source_count,
                            color_scheme="blue",
                            variant="solid",
                        ),
                    ),
                    aria_label="Add or review test context",
                    type="button",
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
                    z_index="60",
                ),
                rx.drawer.content(
                    rx.vstack(
                        rx.hstack(
                            rx.hstack(
                                rx.drawer.title(
                                    "Test context",
                                    font_size="1rem",
                                    font_weight="750",
                                ),
                                rx.badge(
                                    DashboardState.context_source_count,
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
                                    aria_label="Close test context",
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
                            "Attach code, requirements, or an API contract.",
                            color="var(--gray-10)",
                            font_size="0.8rem",
                        ),
                        context_panel(),
                        align="stretch",
                        padding=PANEL_PADDING,
                        spacing="4",
                        width="100%",
                    ),
                    background=PANEL_BACKGROUND,
                    border_left=f"1px solid {BORDER_COLOR}",
                    bottom="0",
                    box_shadow="-12px 0 32px rgba(15, 23, 42, 0.18)",
                    max_width="580px",
                    overflow_y="auto",
                    position="fixed",
                    right="0",
                    top="0",
                    width="94vw",
                    z_index="61",
                ),
            ),
            direction="right",
        )
    )


__all__ = ["context_drawer", "context_panel"]
