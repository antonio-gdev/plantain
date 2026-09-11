"""Existing-test operation preview for Plantain's intent-first workspace."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.scenario_state import ScenarioCatalogState
from plantain.dashboard.shell import action_link, component
from plantain.dashboard.state import DashboardState
from plantain.dashboard.theme import (
    BRAND_BLUE,
    BRAND_YELLOW,
    COMPACT_PANEL_PADDING,
    FOCUS_STYLE,
    MONO_FONT_FAMILY,
    PANEL_PADDING,
    PANEL_STYLE,
    SUBTLE_BACKGROUND,
)


def _status_badge(item: Any) -> rx.Component:
    return component(
        rx.cond(
            item["status"] == "ready",
            rx.badge(
                rx.icon("circle-check", size=14),
                item["status_label"],
                color_scheme="blue",
                variant="soft",
            ),
            rx.badge(
                rx.icon("circle-alert", size=14),
                item["status_label"],
                color_scheme="yellow",
                variant="soft",
            ),
        )
    )


def _navigation_button(
    *,
    icon: str,
    label: str,
    destination: str,
) -> rx.Component:
    return action_link(
        label,
        icon=icon,
        href=destination,
        variant="soft",
    )


def _run_button(item: Any) -> rx.Component:
    active_ids = cast("Any", ScenarioCatalogState.test_active_run_ids)
    active = active_ids.contains(item["scenario_id"])
    return component(
        rx.cond(
            item["status"] == "ready",
            rx.button(
                rx.cond(
                    active,
                    rx.spinner(size="1"),
                    rx.cond(
                        DashboardState.scenario_operation == "rerun",
                        rx.icon("rotate-ccw", size=15),
                        rx.icon("play", size=15),
                    ),
                ),
                rx.cond(
                    active,
                    "In progress",
                    rx.cond(
                        DashboardState.scenario_operation == "rerun",
                        "Run again",
                        "Run test",
                    ),
                ),
                aria_label=rx.cond(
                    DashboardState.scenario_operation == "rerun",
                    item["name"] + " — run test again",
                    item["name"] + " — run test",
                ),
                color_scheme="blue",
                disabled=active,
                on_click=cast("Any", ScenarioCatalogState.run_test)(
                    item["scenario_id"],
                    item["name"],
                ),
                size="2",
                **FOCUS_STYLE,
            ),
            _navigation_button(
                icon="list-checks",
                label="Review test",
                destination="/tests",
            ),
        )
    )


def _scenario_action(item: Any) -> rx.Component:
    return component(
        rx.cond(
            (DashboardState.scenario_operation == "run")
            | (DashboardState.scenario_operation == "rerun"),
            _run_button(item),
            rx.cond(
                DashboardState.scenario_operation == "diagnose",
                _navigation_button(
                    icon="history",
                    label="Review evidence",
                    destination="/runs",
                ),
                _navigation_button(
                    icon="list-checks",
                    label="Open in Tests",
                    destination="/tests",
                ),
            ),
        )
    )


def _scenario_card(item: Any) -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.heading(item["name"], size="4"),
                    rx.hstack(
                        rx.icon("file-text", size=14, color="var(--gray-9)"),
                        rx.text(
                            item["source"],
                            color="var(--gray-10)",
                            font_family=MONO_FONT_FAMILY,
                            font_size="0.74rem",
                        ),
                        align="center",
                        min_width="0",
                        spacing="2",
                    ),
                    align="start",
                    min_width="0",
                    spacing="1",
                ),
                rx.spacer(),
                rx.hstack(
                    _status_badge(item),
                    _scenario_action(item),
                    align="center",
                    spacing="2",
                ),
                align="start",
                direction={"initial": "column", "sm": "row"},
                gap="3",
                width="100%",
            ),
            rx.hstack(
                rx.badge(item["domains"], color_scheme="blue", variant="outline"),
                rx.badge(item["steps"], color_scheme="gray", variant="soft"),
                rx.badge(item["tags"], color_scheme="gray", variant="soft"),
                align="center",
                spacing="2",
                width="100%",
                wrap="wrap",
            ),
            rx.cond(
                item["issue"] != "",
                rx.callout(
                    item["issue"],
                    icon="triangle-alert",
                    color_scheme="yellow",
                    role="status",
                    size="1",
                    width="100%",
                ),
            ),
            align="stretch",
            background=SUBTLE_BACKGROUND,
            border_radius="12px",
            padding=COMPACT_PANEL_PADDING,
            spacing="3",
            width="100%",
        )
    )


def _empty_matches() -> rx.Component:
    return component(
        rx.center(
            rx.vstack(
                rx.box(
                    rx.icon("search-x", size=24, color=BRAND_BLUE),
                    background="var(--blue-3)",
                    border_radius="10px",
                    padding="0.7rem",
                ),
                rx.heading("No ready test matched", size="4"),
                rx.text(
                    "Try a test name, folder, or tag—or browse the complete test library.",
                    color="var(--gray-10)",
                    font_size="0.8rem",
                    max_width="430px",
                    text_align="center",
                ),
                _navigation_button(
                    icon="list-checks",
                    label="Browse Tests",
                    destination="/tests",
                ),
                align="center",
                spacing="3",
            ),
            min_height="190px",
            width="100%",
        )
    )


def scenario_operation_panel() -> rx.Component:
    """Render a confirmation-first operation over locally matched scenarios."""

    return component(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.box(
                        rx.icon("route", size=18, color=BRAND_BLUE),
                        background="var(--blue-3)",
                        border_radius="9px",
                        padding="0.55rem",
                    ),
                    rx.vstack(
                        rx.hstack(
                            rx.heading("Existing tests", size="5"),
                            rx.badge(
                                DashboardState.scenario_operation_label,
                                color_scheme="blue",
                                variant="soft",
                            ),
                            align="center",
                            spacing="2",
                            wrap="wrap",
                        ),
                        rx.text(
                            "Plantain matched your request locally. Confirm the test "
                            "before execution.",
                            color="var(--gray-10)",
                            font_size="0.8rem",
                        ),
                        align="start",
                        spacing="1",
                    ),
                    align="center",
                    spacing="3",
                ),
                rx.spacer(),
                rx.badge(
                    DashboardState.scenario_match_count,
                    "matches",
                    color_scheme="gray",
                    variant="soft",
                ),
                align="center",
                direction={"initial": "column", "sm": "row"},
                gap="3",
                width="100%",
            ),
            rx.hstack(
                rx.text(
                    "Matched from",
                    color="var(--gray-10)",
                    font_size="0.74rem",
                ),
                rx.code(
                    DashboardState.scenario_query,
                    font_family=MONO_FONT_FAMILY,
                    size="1",
                ),
                align="center",
                spacing="2",
                width="100%",
                wrap="wrap",
            ),
            rx.cond(
                DashboardState.scenario_matches_limited,
                rx.callout(
                    "Showing the best matches. Open Tests to browse the complete library.",
                    icon="info",
                    color_scheme="blue",
                    role="status",
                    size="1",
                    width="100%",
                ),
            ),
            rx.cond(
                DashboardState.scenario_match_visible_count < DashboardState.scenario_match_count,
                rx.callout(
                    "Some matching files need attention before they can be used.",
                    icon="triangle-alert",
                    color_scheme="yellow",
                    role="status",
                    size="1",
                    width="100%",
                ),
            ),
            rx.cond(
                DashboardState.scenario_match_visible_count > 0,
                rx.vstack(
                    rx.foreach(
                        DashboardState.scenario_matches,
                        _scenario_card,
                    ),
                    align="stretch",
                    spacing="3",
                    width="100%",
                ),
                _empty_matches(),
            ),
            rx.cond(
                (DashboardState.scenario_operation == "run")
                | (DashboardState.scenario_operation == "rerun"),
                rx.hstack(
                    rx.icon("shield-check", size=14, color=BRAND_YELLOW),
                    rx.text(
                        "You can start multiple tests; Plantain applies safe shared "
                        "resource limits automatically.",
                        color="var(--gray-10)",
                        font_size="0.74rem",
                    ),
                    align="center",
                    spacing="2",
                    width="100%",
                ),
            ),
            align="stretch",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        )
    )


__all__ = ["scenario_operation_panel"]
