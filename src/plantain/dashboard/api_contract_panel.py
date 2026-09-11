"""Safe API-contract operation selection for the intent-first workspace."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.shell import component
from plantain.dashboard.state import DashboardState
from plantain.dashboard.theme import (
    BRAND_BLUE,
    BRAND_YELLOW,
    FOCUS_STYLE,
    MONO_FONT_FAMILY,
    PANEL_STYLE,
)


def _detail(
    *,
    icon: str,
    label: str,
    value: Any,
    monospace: bool = False,
) -> rx.Component:
    return component(
        rx.hstack(
            rx.icon(icon, size=14, color="var(--gray-9)"),
            rx.text(
                label,
                color="var(--gray-10)",
                font_size="0.72rem",
                font_weight="650",
            ),
            rx.text(
                value,
                color="var(--gray-12)",
                font_family=MONO_FONT_FAMILY if monospace else "inherit",
                font_size="0.74rem",
                overflow="hidden",
                text_overflow="ellipsis",
                white_space="nowrap",
            ),
            align="center",
            min_width="0",
            spacing="2",
            width="100%",
        )
    )


def _operation_card(item: Any) -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.badge(
                        item["method"],
                        color_scheme="blue",
                        variant="outline",
                    ),
                    rx.text(
                        item["path"],
                        color="var(--gray-12)",
                        font_family=MONO_FONT_FAMILY,
                        font_size="0.8rem",
                        font_weight="650",
                        overflow_wrap="anywhere",
                    ),
                    align="center",
                    min_width="0",
                    spacing="2",
                ),
                rx.spacer(),
                rx.button(
                    rx.cond(
                        DashboardState.agent_busy,
                        rx.spinner(size="1"),
                        rx.icon("wand-sparkles", size=15),
                    ),
                    rx.cond(
                        DashboardState.agent_busy,
                        "Preparing",
                        "Create test",
                    ),
                    aria_label=(item["method"] + " " + item["path"] + " — create test"),
                    color_scheme="blue",
                    disabled=DashboardState.agent_busy,
                    on_click=cast("Any", DashboardState.select_api_operation)(
                        item["operation_key"]
                    ),
                    size="2",
                    **FOCUS_STYLE,
                ),
                align="start",
                direction={"initial": "column", "sm": "row"},
                gap="3",
                width="100%",
            ),
            rx.hstack(
                rx.icon("fingerprint", size=13, color="var(--gray-9)"),
                rx.text(
                    item["operation_id"],
                    color="var(--gray-10)",
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.72rem",
                    overflow_wrap="anywhere",
                ),
                align="center",
                spacing="2",
                width="100%",
            ),
            rx.cond(
                item["summary"] != "",
                rx.text(
                    item["summary"],
                    color="var(--gray-10)",
                    font_size="0.78rem",
                    line_height="1.45",
                ),
            ),
            rx.cond(
                item["display_limited"],
                rx.badge(
                    "Long metadata shortened",
                    color_scheme="gray",
                    variant="soft",
                ),
            ),
            align="stretch",
            background="var(--gray-1)",
            border="1px solid var(--gray-5)",
            border_radius="12px",
            padding="1rem",
            spacing="3",
            width="100%",
        )
    )


def _empty_state() -> rx.Component:
    return component(
        rx.center(
            rx.vstack(
                rx.box(
                    rx.icon("search-x", size=23, color=BRAND_YELLOW),
                    background="var(--yellow-3)",
                    border_radius="10px",
                    padding="0.7rem",
                ),
                rx.heading("No operation matched yet", size="4"),
                rx.text(
                    "Refine your request with the behavior, path, or operation name. "
                    "Plantain will inspect the same contract again.",
                    color="var(--gray-10)",
                    font_size="0.8rem",
                    max_width="460px",
                    text_align="center",
                ),
                align="center",
                spacing="2",
            ),
            min_height="180px",
            width="100%",
        )
    )


def api_contract_panel() -> rx.Component:
    """Render safe local schema metadata and any required operation choice."""

    return component(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.box(
                        rx.icon("braces", size=18, color=BRAND_BLUE),
                        background="var(--blue-3)",
                        border_radius="9px",
                        padding="0.55rem",
                    ),
                    rx.vstack(
                        rx.hstack(
                            rx.heading("API contract", size="5"),
                            rx.badge(
                                "Locally inspected",
                                color_scheme="blue",
                                variant="soft",
                            ),
                            rx.badge(
                                DashboardState.api_schema_version,
                                color_scheme="gray",
                                variant="outline",
                            ),
                            align="center",
                            spacing="2",
                            wrap="wrap",
                        ),
                        rx.text(
                            "Plantain grounded your request in the contract. "
                            "Choose only when more than one operation is plausible.",
                            color="var(--gray-10)",
                            font_size="0.8rem",
                        ),
                        align="start",
                        spacing="1",
                    ),
                    align="center",
                    min_width="0",
                    spacing="3",
                ),
                rx.spacer(),
                rx.badge(
                    DashboardState.api_operation_match_count,
                    " matches",
                    color_scheme="gray",
                    variant="soft",
                ),
                align="start",
                direction={"initial": "column", "md": "row"},
                gap="3",
                width="100%",
            ),
            rx.grid(
                rx.cond(
                    DashboardState.api_operation_query != "",
                    _detail(
                        icon="search",
                        label="Intent",
                        value=DashboardState.api_operation_query,
                    ),
                ),
                rx.cond(
                    DashboardState.api_source_url != "",
                    _detail(
                        icon="link",
                        label="Contract",
                        value=DashboardState.api_source_url,
                        monospace=True,
                    ),
                ),
                rx.cond(
                    DashboardState.api_base_url != "",
                    _detail(
                        icon="server",
                        label="Base URL",
                        value=DashboardState.api_base_url,
                        monospace=True,
                    ),
                ),
                columns={"initial": "1", "lg": "2"},
                gap="2",
                width="100%",
            ),
            rx.cond(
                DashboardState.api_operation_visible_count > 0,
                rx.grid(
                    rx.foreach(
                        DashboardState.api_operations,
                        _operation_card,
                    ),
                    columns={"initial": "1", "xl": "2"},
                    gap="3",
                    width="100%",
                ),
                _empty_state(),
            ),
            rx.cond(
                DashboardState.api_operation_matches_limited,
                rx.callout(
                    "Only the strongest bounded matches are shown. Add a path or "
                    "operation name to narrow the choice.",
                    icon="info",
                    color_scheme="yellow",
                    role="status",
                    size="1",
                    width="100%",
                ),
            ),
            rx.callout(
                "Your selection creates schema-grounded YAML for review before "
                "anything is saved or run.",
                icon="shield-check",
                color_scheme="blue",
                role="status",
                size="1",
                width="100%",
            ),
            align="stretch",
            padding={"initial": "1rem", "md": "1.2rem"},
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        )
    )


__all__ = ["api_contract_panel"]
