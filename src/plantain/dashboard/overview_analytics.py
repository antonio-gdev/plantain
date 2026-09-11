"""Responsive quality and agent-usage surfaces for the local overview."""

from __future__ import annotations

from typing import Any

import reflex as rx

from plantain.dashboard.shell import component as _component
from plantain.dashboard.state import DashboardState
from plantain.dashboard.theme import (
    BRAND_BLUE,
    BRAND_RED,
    BRAND_YELLOW,
    FOCUS_STYLE,
    PANEL_STYLE,
)


def _progress_track(
    progress_width: Any,
    progress_percent: Any,
    *,
    aria_label: str,
    track_background: str = "var(--gray-a4)",
) -> rx.Component:
    return _component(
        rx.box(
            rx.box(
                background=BRAND_BLUE,
                border_radius="inherit",
                height="100%",
                transition="width 180ms ease",
                width=progress_width,
            ),
            aria_label=aria_label,
            aria_valuemax=100,
            aria_valuemin=0,
            aria_valuenow=progress_percent,
            background=track_background,
            border_radius="999px",
            height="0.3rem",
            overflow="hidden",
            role="progressbar",
            width="100%",
        )
    )


def _panel_heading(
    title: str,
    description: str,
    *,
    icon: str,
    accent: str,
    trailing: rx.Component | None = None,
) -> rx.Component:
    children: list[rx.Component] = [
        rx.box(
            rx.icon(icon, size=18, color=accent),
            background="var(--gray-3)",
            border_radius="9px",
            padding="0.55rem",
        ),
        rx.vstack(
            rx.heading(title, size="4"),
            rx.text(
                description,
                color="var(--gray-10)",
                font_size="0.78rem",
            ),
            align="start",
            spacing="1",
        ),
        rx.spacer(),
    ]
    if trailing is not None:
        children.append(trailing)
    return _component(
        rx.hstack(
            *children,
            align="center",
            width="100%",
        )
    )


def _loading_state(label: str) -> rx.Component:
    return _component(
        rx.center(
            rx.hstack(
                rx.spinner(),
                rx.text(label, color="var(--gray-10)", font_size="0.8rem"),
                align="center",
                role="status",
                spacing="2",
            ),
            min_height="210px",
            width="100%",
        )
    )


def _empty_state(
    title: str,
    description: str,
    *,
    href: str,
    action: str,
) -> rx.Component:
    return _component(
        rx.center(
            rx.vstack(
                rx.box(
                    rx.icon("history", size=21, color=BRAND_BLUE),
                    background="var(--blue-3)",
                    border_radius="10px",
                    padding="0.65rem",
                ),
                rx.text(title, font_weight="650"),
                rx.text(
                    description,
                    color="var(--gray-10)",
                    font_size="0.78rem",
                    max_width="330px",
                    text_align="center",
                ),
                rx.link(
                    action,
                    href=href,
                    color=BRAND_BLUE,
                    font_size="0.8rem",
                    font_weight="650",
                    text_decoration="none",
                    **FOCUS_STYLE,
                ),
                align="center",
                spacing="2",
            ),
            min_height="210px",
            width="100%",
        )
    )


def _status_total(
    label: str,
    value: Any,
    *,
    accent: str,
    icon: str,
) -> rx.Component:
    return _component(
        rx.hstack(
            rx.icon(icon, size=16, color=accent),
            rx.vstack(
                rx.text(value, font_size="1.1rem", font_weight="700"),
                rx.text(label, color="var(--gray-10)", font_size="0.7rem"),
                align="start",
                spacing="0",
            ),
            align="center",
            background="var(--gray-2)",
            border="1px solid var(--gray-4)",
            border_radius="9px",
            padding="0.7rem",
            width="100%",
        )
    )


def _quality_content() -> rx.Component:
    return _component(
        rx.vstack(
            rx.grid(
                rx.vstack(
                    rx.hstack(
                        rx.text(
                            DashboardState.quality_pass_rate_percent,
                            font_size="2rem",
                            font_weight="750",
                            line_height="1",
                        ),
                        rx.text("%", color="var(--gray-10)", font_weight="650"),
                        align="end",
                        spacing="1",
                    ),
                    rx.text(
                        "Pass rate",
                        color="var(--gray-10)",
                        font_size="0.75rem",
                    ),
                    _progress_track(
                        DashboardState.quality_rate_width,
                        DashboardState.quality_pass_rate_percent,
                        aria_label="Pass rate across analyzed run results",
                        track_background=BRAND_RED,
                    ),
                    rx.text(
                        DashboardState.quality_analyzed_run_count,
                        " valid results analyzed",
                        color="var(--gray-9)",
                        font_size="0.7rem",
                    ),
                    align="stretch",
                    background="var(--gray-2)",
                    border_radius="10px",
                    padding="0.9rem",
                    spacing="2",
                    width="100%",
                ),
                rx.grid(
                    _status_total(
                        "Passed",
                        DashboardState.quality_passed_count,
                        accent=BRAND_BLUE,
                        icon="check",
                    ),
                    _status_total(
                        "Failed",
                        DashboardState.quality_failed_count,
                        accent=BRAND_RED,
                        icon="x",
                    ),
                    _status_total(
                        "Cancelled",
                        DashboardState.quality_cancelled_count,
                        accent=BRAND_YELLOW,
                        icon="minus",
                    ),
                    columns="1",
                    gap="2",
                    width="100%",
                ),
                columns={"initial": "1", "sm": "2"},
                gap="3",
                width="100%",
            ),
            rx.grid(
                rx.vstack(
                    rx.text(
                        DashboardState.quality_average_duration,
                        font_weight="700",
                    ),
                    rx.text(
                        "Average reported duration",
                        color="var(--gray-10)",
                        font_size="0.7rem",
                    ),
                    align="start",
                    spacing="0",
                ),
                rx.vstack(
                    rx.text(
                        DashboardState.quality_mixed_outcome_count,
                        font_weight="700",
                    ),
                    rx.text(
                        "Scenarios with both pass and fail outcomes",
                        color="var(--gray-10)",
                        font_size="0.7rem",
                    ),
                    align="start",
                    spacing="0",
                ),
                border_top="1px solid var(--gray-4)",
                columns={"initial": "1", "sm": "2"},
                gap="3",
                padding_top="0.8rem",
                width="100%",
            ),
            rx.text(
                "Mixed outcomes describe bounded local history; they are not labeled as flakiness.",
                color="var(--gray-9)",
                font_size="0.68rem",
            ),
            align="stretch",
            spacing="3",
            width="100%",
        )
    )


def _quality_panel() -> rx.Component:
    return _component(
        rx.vstack(
            _panel_heading(
                "Quality snapshot",
                "Signals derived from valid native Plantain results",
                icon="list-checks",
                accent=BRAND_BLUE,
            ),
            rx.cond(
                DashboardState.is_loading,
                _loading_state("Analyzing local results…"),
                rx.cond(
                    DashboardState.quality_available,
                    _quality_content(),
                    _empty_state(
                        "No quality history yet",
                        "Run a scenario to build an honest local quality snapshot.",
                        href="/tests",
                        action="Choose a test",
                    ),
                ),
            ),
            align="stretch",
            padding="1rem",
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        )
    )


def _usage_breakdown_row(row: Any) -> rx.Component:
    return _component(
        rx.hstack(
            rx.vstack(
                rx.text(row["provider"], font_size="0.8rem", font_weight="650"),
                rx.text(
                    row["model"],
                    color="var(--gray-9)",
                    font_family="monospace",
                    font_size="0.68rem",
                ),
                align="start",
                min_width="0",
                spacing="0",
            ),
            rx.spacer(),
            rx.vstack(
                rx.text(row["calls"], " calls", font_size="0.75rem"),
                rx.hstack(
                    rx.text(row["metered"]),
                    rx.text("·"),
                    rx.text(row["tokens"], " tokens"),
                    color="var(--gray-9)",
                    font_size="0.65rem",
                    spacing="1",
                ),
                align="end",
                spacing="0",
            ),
            align="center",
            border_bottom="1px solid var(--gray-4)",
            padding_y="0.55rem",
            width="100%",
        )
    )


def _usage_trend_row(row: Any) -> rx.Component:
    return _component(
        rx.grid(
            rx.text(row["label"], color="var(--gray-9)", font_size="0.68rem"),
            _progress_track(
                row["activity_width"],
                row["activity_percent"],
                aria_label="Relative daily agent activity",
            ),
            rx.vstack(
                rx.text(row["calls"], " calls", font_size="0.68rem"),
                rx.text(
                    row["tokens"],
                    " tokens",
                    color="var(--gray-9)",
                    font_size="0.62rem",
                ),
                align="end",
                spacing="0",
            ),
            align_items="center",
            columns="62px minmax(80px, 1fr) 72px",
            gap="2",
            width="100%",
        )
    )


def _usage_content() -> rx.Component:
    return _component(
        rx.vstack(
            rx.grid(
                _status_total(
                    "Provider calls",
                    DashboardState.usage_call_count,
                    accent=BRAND_BLUE,
                    icon="activity",
                ),
                _status_total(
                    "Metered calls",
                    DashboardState.usage_metered_call_count,
                    accent=BRAND_YELLOW,
                    icon="check",
                ),
                _status_total(
                    "Reported tokens",
                    DashboardState.usage_total_tokens,
                    accent=BRAND_RED,
                    icon="list-checks",
                ),
                columns={"initial": "1", "sm": "3"},
                gap="2",
                width="100%",
            ),
            rx.hstack(
                rx.text("Input", color="var(--gray-9)", font_size="0.7rem"),
                rx.text(DashboardState.usage_input_tokens, font_weight="650"),
                rx.text("·", color="var(--gray-8)"),
                rx.text("Output", color="var(--gray-9)", font_size="0.7rem"),
                rx.text(DashboardState.usage_output_tokens, font_weight="650"),
                align="center",
                spacing="2",
            ),
            rx.callout(
                rx.vstack(
                    rx.text(
                        DashboardState.usage_cost_boundary,
                        font_weight="650",
                    ),
                    rx.text(
                        "Plantain does not estimate currency cost. "
                        "Provider billing or your local runtime is the source of truth.",
                        font_size="0.72rem",
                    ),
                    align="start",
                    spacing="1",
                ),
                color_scheme="blue",
                icon="info",
                role="note",
                width="100%",
            ),
            rx.vstack(
                rx.text(
                    "Provider and model",
                    color="var(--gray-9)",
                    font_size="0.68rem",
                    font_weight="700",
                    letter_spacing="0.06em",
                ),
                rx.foreach(DashboardState.usage_breakdowns, _usage_breakdown_row),
                align="stretch",
                spacing="0",
                width="100%",
            ),
            rx.vstack(
                rx.text(
                    "Last 7 UTC days",
                    color="var(--gray-9)",
                    font_size="0.68rem",
                    font_weight="700",
                    letter_spacing="0.06em",
                ),
                rx.foreach(DashboardState.usage_trend, _usage_trend_row),
                align="stretch",
                spacing="2",
                width="100%",
            ),
            align="stretch",
            spacing="3",
            width="100%",
        )
    )


def _usage_panel() -> rx.Component:
    return _component(
        rx.vstack(
            _panel_heading(
                "Agent usage",
                "Exact metadata reported by configured providers",
                icon="activity",
                accent=BRAND_YELLOW,
                trailing=rx.badge(
                    DashboardState.usage_cost_boundary,
                    color_scheme="blue",
                    variant="outline",
                ),
            ),
            rx.cond(
                DashboardState.usage_history_notice != "",
                rx.callout(
                    DashboardState.usage_history_notice,
                    color_scheme="yellow",
                    icon="info",
                    role="status",
                    width="100%",
                ),
            ),
            rx.cond(
                DashboardState.is_loading,
                _loading_state("Loading private usage history…"),
                rx.cond(
                    DashboardState.usage_history_available,
                    _usage_content(),
                    _empty_state(
                        "No agent usage yet",
                        "Successful agent calls appear here; token totals "
                        "remain unavailable when a provider omits them.",
                        href="/settings",
                        action="Configure an agent",
                    ),
                ),
            ),
            align="stretch",
            padding="1rem",
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        )
    )


def overview_analytics() -> rx.Component:
    """Render responsive quality and provider-usage summaries."""

    return _component(
        rx.grid(
            _quality_panel(),
            _usage_panel(),
            align_items="start",
            columns={"initial": "1", "xl": "2"},
            gap="4",
            width="100%",
        )
    )


__all__ = ["overview_analytics"]
