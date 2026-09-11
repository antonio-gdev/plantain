"""Run-history controls and compact immutable-result list."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.run_state import RunCatalogState
from plantain.dashboard.shell import action_link, component
from plantain.dashboard.theme import (
    BRAND_BLUE,
    BRAND_RED,
    FOCUS_STYLE,
    MONO_FONT_FAMILY,
    PANEL_PADDING,
    PANEL_STYLE,
)


def run_status_badge(status: Any, label: Any) -> rx.Component:
    """Render consistent human-facing run status."""

    return component(
        rx.cond(
            status == "passed",
            rx.badge(
                rx.icon("circle-check", size=13),
                label,
                color_scheme="blue",
                variant="soft",
            ),
            rx.cond(
                status == "failed",
                rx.badge(
                    rx.icon("circle-alert", size=13),
                    label,
                    color_scheme="red",
                    variant="soft",
                ),
                rx.badge(
                    rx.icon("circle-pause", size=13),
                    label,
                    color_scheme="yellow",
                    variant="soft",
                ),
            ),
        )
    )


def _filter_button(
    label: str,
    value: str,
    color_scheme: str,
) -> rx.Component:
    handler = cast("Any", RunCatalogState.filter_run_history)
    common = {
        "aria_label": f"Show {label.lower()} runs",
        "color_scheme": cast("Any", color_scheme),
        "disabled": RunCatalogState.history_loading,
        "on_click": handler(value),
        "size": "2",
        **FOCUS_STYLE,
    }
    return component(
        rx.cond(
            RunCatalogState.history_status_filter == value,
            rx.button(label, variant="solid", **common),
            rx.button(label, variant="soft", **common),
        )
    )


def history_controls() -> rx.Component:
    """Render status filters and one bounded metadata search."""

    return component(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    _filter_button("All", "all", "blue"),
                    _filter_button("Passed", "passed", "blue"),
                    _filter_button("Failed", "failed", "red"),
                    _filter_button("Stopped", "cancelled", "yellow"),
                    align="center",
                    spacing="2",
                    wrap="wrap",
                ),
                rx.spacer(),
                rx.form(
                    rx.flex(
                        rx.input(
                            name="query",
                            aria_label="Search run history",
                            max_length=200,
                            placeholder="Search scenario, source, tag, or activity",
                            width={"initial": "100%", "sm": "320px"},
                            **FOCUS_STYLE,
                        ),
                        rx.button(
                            rx.icon("search", size=15),
                            "Search",
                            aria_label="Search run history",
                            color_scheme="blue",
                            type="submit",
                            variant="soft",
                            **FOCUS_STYLE,
                        ),
                        align={"initial": "stretch", "sm": "center"},
                        direction={"initial": "column", "sm": "row"},
                        gap="2",
                        width={"initial": "100%", "sm": "auto"},
                    ),
                    on_submit=RunCatalogState.search_run_history,
                    reset_on_submit=True,
                    width={"initial": "100%", "sm": "auto"},
                ),
                align={"initial": "stretch", "md": "center"},
                direction={"initial": "column", "md": "row"},
                gap="3",
                width="100%",
            ),
            rx.cond(
                RunCatalogState.history_query != "",
                rx.hstack(
                    rx.text(
                        "Search:",
                        color="var(--gray-10)",
                        font_size="0.74rem",
                    ),
                    rx.badge(
                        RunCatalogState.history_query,
                        color_scheme="blue",
                        variant="soft",
                    ),
                    rx.icon_button(
                        rx.icon("x", size=13),
                        aria_label="Clear run search",
                        on_click=RunCatalogState.clear_run_search,
                        size="1",
                        variant="ghost",
                        **FOCUS_STYLE,
                    ),
                    align="center",
                    spacing="1",
                ),
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


def _run_card(item: Any) -> rx.Component:
    selected = RunCatalogState.history_selected_run_id == item["run_id"]
    return component(
        rx.button(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        run_status_badge(item["status"], item["status_label"]),
                        rx.text(
                            item["scenario"],
                            font_size="0.9rem",
                            font_weight="720",
                        ),
                        align="center",
                        min_width="0",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.spacer(),
                    rx.hstack(
                        rx.text(
                            item["short_id"],
                            color="var(--gray-9)",
                            font_family=MONO_FONT_FAMILY,
                            font_size="0.68rem",
                        ),
                        rx.icon(
                            "chevron-right",
                            color=rx.cond(selected, BRAND_BLUE, "var(--gray-8)"),
                            size=16,
                        ),
                        align="center",
                        spacing="1",
                    ),
                    align="center",
                    gap="2",
                    width="100%",
                ),
                rx.text(
                    item["source"],
                    color="var(--gray-10)",
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.7rem",
                    overflow="hidden",
                    text_overflow="ellipsis",
                    white_space="nowrap",
                    width="100%",
                ),
                rx.hstack(
                    rx.text(item["started"]),
                    rx.text("·"),
                    rx.text(item["duration"]),
                    rx.text("·"),
                    rx.text(item["steps"]),
                    color="var(--gray-10)",
                    font_size="0.72rem",
                    spacing="2",
                    wrap="wrap",
                ),
                rx.hstack(
                    rx.badge(item["tags"], color_scheme="gray", variant="soft"),
                    rx.badge(
                        item["integrations"],
                        color_scheme="gray",
                        variant="outline",
                    ),
                    spacing="2",
                    wrap="wrap",
                ),
                rx.cond(
                    item["failure"] != "",
                    rx.hstack(
                        rx.icon("triangle-alert", color=BRAND_RED, size=14),
                        rx.text(
                            item["failure"],
                            color="var(--red-11)",
                            font_size="0.72rem",
                            font_weight="650",
                        ),
                        rx.badge(
                            item["failure_type"],
                            color_scheme="red",
                            variant="soft",
                        ),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                ),
                align="start",
                spacing="2",
                width="100%",
            ),
            aria_label=item["scenario"] + " — open run summary",
            aria_pressed=selected,
            background=rx.cond(selected, "var(--blue-3)", "var(--gray-2)"),
            border=rx.cond(
                selected,
                f"1px solid {BRAND_BLUE}",
                "1px solid var(--gray-5)",
            ),
            border_radius="12px",
            color="var(--gray-12)",
            cursor="pointer",
            height="auto",
            justify_content="flex-start",
            on_click=cast("Any", RunCatalogState.select_run)(item["run_id"]),
            padding="0.9rem",
            text_align="left",
            variant="ghost",
            white_space="normal",
            width="100%",
            **FOCUS_STYLE,
        )
    )


def _empty_history() -> rx.Component:
    filtered = (RunCatalogState.history_query != "") | (
        RunCatalogState.history_status_filter != "all"
    )
    return component(
        rx.center(
            rx.vstack(
                rx.box(
                    rx.icon("history", color=BRAND_BLUE, size=24),
                    background="var(--blue-3)",
                    border_radius="12px",
                    padding="0.8rem",
                ),
                rx.heading(
                    rx.cond(filtered, "No runs match these filters", "No run history yet"),
                    size="5",
                ),
                rx.text(
                    rx.cond(
                        filtered,
                        "Adjust the status or search terms to broaden the result set.",
                        "Run a test to create its first immutable local result.",
                    ),
                    color="var(--gray-10)",
                    max_width="380px",
                    text_align="center",
                ),
                action_link(
                    "Go to tests",
                    icon="play",
                    href="/tests",
                    variant="soft",
                ),
                align="center",
                spacing="3",
            ),
            min_height="300px",
            padding="2rem",
            width="100%",
        )
    )


def _pagination() -> rx.Component:
    return component(
        rx.cond(
            RunCatalogState.history_total > 0,
            rx.hstack(
                rx.button(
                    rx.icon("chevron-left", size=16),
                    "Previous",
                    aria_label="Previous runs page",
                    disabled=(
                        ~RunCatalogState.history_has_previous | RunCatalogState.history_loading
                    ),
                    on_click=RunCatalogState.previous_run_page,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                rx.spacer(),
                rx.text(
                    "Page ",
                    RunCatalogState.history_page,
                    " of ",
                    rx.cond(
                        RunCatalogState.history_page_count > 0,
                        RunCatalogState.history_page_count,
                        1,
                    ),
                    color="var(--gray-10)",
                    font_size="0.76rem",
                    role="status",
                ),
                rx.spacer(),
                rx.button(
                    "Next",
                    rx.icon("chevron-right", size=16),
                    aria_label="Next runs page",
                    disabled=(~RunCatalogState.history_has_next | RunCatalogState.history_loading),
                    on_click=RunCatalogState.next_run_page,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                align="center",
                padding="0.8rem",
                width="100%",
            ),
        )
    )


def history_panel() -> rx.Component:
    """Render one bounded result page with deterministic selection."""

    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.text(
                        "RUN HISTORY",
                        color="var(--gray-9)",
                        font_size="0.68rem",
                        font_weight="700",
                        letter_spacing="0.1em",
                    ),
                    rx.heading("Completed runs", size="4"),
                    align="start",
                    spacing="1",
                ),
                rx.spacer(),
                rx.badge(
                    RunCatalogState.history_total,
                    rx.cond(RunCatalogState.history_total_limited, "+", ""),
                    color_scheme="blue",
                    variant="soft",
                ),
                align="center",
                padding=PANEL_PADDING,
                width="100%",
            ),
            rx.cond(
                RunCatalogState.history_loading,
                rx.center(
                    rx.vstack(
                        rx.spinner(size="3"),
                        rx.text(
                            "Reading local run history…",
                            color="var(--gray-10)",
                            font_size="0.8rem",
                        ),
                        align="center",
                        spacing="2",
                    ),
                    min_height="300px",
                    role="status",
                    width="100%",
                ),
                rx.cond(
                    RunCatalogState.history_total == 0,
                    _empty_history(),
                    rx.vstack(
                        rx.foreach(RunCatalogState.history_items, _run_card),
                        align="stretch",
                        padding="0.8rem",
                        spacing="2",
                        width="100%",
                    ),
                ),
            ),
            _pagination(),
            align="stretch",
            overflow="hidden",
            spacing="0",
            width="100%",
            **PANEL_STYLE,
        )
    )


def history_feedback() -> rx.Component:
    """Render value-free run-history errors and bounded-scan notices."""

    return component(
        rx.vstack(
            rx.cond(
                RunCatalogState.history_error != "",
                rx.callout(
                    RunCatalogState.history_error,
                    icon="circle-alert",
                    color_scheme="red",
                    role="alert",
                    width="100%",
                ),
            ),
            rx.cond(
                RunCatalogState.history_notice != "",
                rx.callout(
                    RunCatalogState.history_notice,
                    icon="info",
                    color_scheme="yellow",
                    role="status",
                    width="100%",
                ),
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


__all__ = [
    "history_controls",
    "history_feedback",
    "history_panel",
    "run_status_badge",
]
