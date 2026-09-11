"""Reflex application entry point for Plantain's local dashboard."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.create_page import create_page
from plantain.dashboard.overview_analytics import overview_analytics
from plantain.dashboard.run_state import RunCatalogState
from plantain.dashboard.runs_page import runs_page
from plantain.dashboard.scenario_state import ScenarioCatalogState
from plantain.dashboard.settings_page import settings_page
from plantain.dashboard.shell import component as _component
from plantain.dashboard.shell import page_shell
from plantain.dashboard.state import DashboardState
from plantain.dashboard.tests_page import tests_page
from plantain.dashboard.theme import (
    APP_STYLE,
    BRAND_BLUE,
    BRAND_RED,
    BRAND_YELLOW,
    FOCUS_STYLE,
    PANEL_STYLE,
)


def _metric_card(
    label: str,
    value: Any,
    description: str,
    *,
    icon: str,
    accent: str,
    limited: Any = False,
) -> rx.Component:
    component = rx.vstack(
        rx.hstack(
            rx.box(
                rx.icon(icon, size=19, color=accent),
                background="var(--gray-3)",
                border_radius="9px",
                padding="0.55rem",
            ),
            rx.spacer(),
            rx.text(
                label.upper(),
                color="var(--gray-9)",
                font_size="0.68rem",
                font_weight="700",
                letter_spacing="0.08em",
            ),
            align="center",
            width="100%",
        ),
        rx.hstack(
            rx.text(value, font_size="2rem", font_weight="750", line_height="1"),
            rx.cond(
                limited,
                rx.text(
                    "+",
                    aria_label="Count is a lower bound",
                    font_size="1.25rem",
                    font_weight="750",
                ),
            ),
            align="end",
            spacing="0",
        ),
        rx.text(
            description,
            color="var(--gray-10)",
            font_size="0.78rem",
        ),
        align="stretch",
        padding="1rem",
        spacing="3",
        width="100%",
        **PANEL_STYLE,
    )
    return _component(component)


def _status_badge(status: Any) -> rx.Component:
    component = rx.cond(
        status == "passed",
        rx.badge("Passed", color_scheme="blue", variant="soft"),
        rx.cond(
            status == "failed",
            rx.badge("Failed", color_scheme="red", variant="soft"),
            rx.badge("Cancelled", color_scheme="yellow", variant="soft"),
        ),
    )
    return _component(component)


def _run_row(run: Any) -> rx.Component:
    component = rx.hstack(
        rx.vstack(
            rx.text(
                run["scenario"],
                font_size="0.88rem",
                font_weight="650",
            ),
            rx.text(
                run["completed"],
                color="var(--gray-9)",
                font_size="0.72rem",
            ),
            align="start",
            min_width="0",
            spacing="1",
        ),
        rx.spacer(),
        rx.text(
            run["duration"],
            color="var(--gray-10)",
            font_family="monospace",
            font_size="0.75rem",
        ),
        _status_badge(run["status"]),
        align="center",
        border_bottom="1px solid var(--gray-4)",
        min_height="64px",
        padding_y="0.7rem",
        width="100%",
    )
    return _component(component)


def _empty_runs() -> rx.Component:
    component = rx.center(
        rx.vstack(
            rx.box(
                rx.icon("history", size=22, color=BRAND_BLUE),
                background="var(--blue-3)",
                border_radius="10px",
                padding="0.65rem",
            ),
            rx.text("No run results yet", font_weight="650"),
            rx.text(
                "Runs started from the CLI or dashboard will appear here.",
                color="var(--gray-10)",
                font_size="0.8rem",
                text_align="center",
            ),
            align="center",
            max_width="310px",
            spacing="2",
        ),
        min_height="190px",
        width="100%",
    )
    return _component(component)


def _recent_runs_panel() -> rx.Component:
    component = rx.vstack(
        rx.hstack(
            rx.vstack(
                rx.heading("Recent runs", size="4"),
                rx.text(
                    "Latest native Plantain result summaries",
                    color="var(--gray-10)",
                    font_size="0.78rem",
                ),
                align="start",
                spacing="1",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("refresh-cw", size=15),
                "Refresh",
                variant="soft",
                on_click=DashboardState.refresh,
                loading=DashboardState.is_loading,
                **FOCUS_STYLE,
            ),
            align="center",
            width="100%",
        ),
        rx.cond(
            DashboardState.run_count == 0,
            rx.cond(
                DashboardState.run_count_limited,
                rx.center(
                    rx.text(
                        "No valid run summary was found in the scanned portion.",
                        color="var(--gray-10)",
                        font_size="0.8rem",
                    ),
                    min_height="190px",
                ),
                _empty_runs(),
            ),
            rx.vstack(
                rx.foreach(DashboardState.recent_runs, _run_row),
                align="stretch",
                spacing="0",
                width="100%",
            ),
        ),
        align="stretch",
        padding="1rem",
        spacing="3",
        width="100%",
        **PANEL_STYLE,
    )
    return _component(component)


def _workspace_notice() -> rx.Component:
    component = rx.vstack(
        rx.cond(
            DashboardState.error_message != "",
            rx.callout(
                DashboardState.error_message,
                icon="triangle-alert",
                color_scheme="red",
                role="alert",
                width="100%",
            ),
        ),
        rx.cond(
            DashboardState.notice != "",
            rx.callout(
                DashboardState.notice,
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
    return _component(component)


def index() -> rx.Component:
    """Render the real local-workspace overview without fabricated data."""

    content = rx.vstack(
        rx.flex(
            rx.vstack(
                rx.heading(
                    "OVERVIEW",
                    size="4",
                    letter_spacing="-0.025em",
                ),
                align="start",
            ),
            rx.spacer(),
            rx.badge(
                "Privacy-first",
                color_scheme="blue",
                size="2",
                variant="outline",
            ),
            align={"initial": "start", "sm": "end"},
            direction={"initial": "column", "sm": "row"},
            gap="3",
            width="100%",
        ),
        _workspace_notice(),
        rx.grid(
            _metric_card(
                "Scenarios",
                DashboardState.scenario_count,
                "Reviewable YAML definitions",
                icon="list-checks",
                accent=BRAND_BLUE,
            ),
            _metric_card(
                "Runs and Results",
                DashboardState.run_count,
                "Locally persisted results",
                icon="history",
                accent=BRAND_YELLOW,
                limited=DashboardState.run_count_limited,
            ),
            _metric_card(
                "Evidence",
                DashboardState.evidence_count,
                "Private semantic manifests",
                icon="file-search",
                accent=BRAND_RED,
                limited=DashboardState.evidence_count_limited,
            ),
            columns={"initial": "1", "md": "3"},
            gap="4",
            width="100%",
        ),
        overview_analytics(),
        _recent_runs_panel(),
        align="stretch",
        max_width="1180px",
        spacing="5",
        width="100%",
    )
    return page_shell(
        _component(content),
        active="overview",
        title="Overview",
    )


app = rx.App(
    style=cast("Any", APP_STYLE),
    head_components=[
        rx.el.link(rel="icon", href="/plantain_favicon.png"),
        rx.el.meta(
            name="description",
            content="Plantain privacy-first local testing dashboard",
        ),
        rx.el.meta(name="theme-color", content=BRAND_BLUE),
    ],
)
app.add_page(
    index,
    route="/",
    title="Plantain · Local testing workspace",
    on_load=DashboardState.refresh,
)
app.add_page(
    create_page,
    route="/create",
    title="Create · Plantain",
    on_load=DashboardState.refresh,
)
app.add_page(
    tests_page,
    route="/tests",
    title="Tests · Plantain",
    on_load=ScenarioCatalogState.refresh_tests,
)
app.add_page(
    runs_page,
    route="/runs",
    title="Runs and Results · Plantain",
    on_load=RunCatalogState.refresh_run_history,
)
app.add_page(
    settings_page,
    route="/settings",
    title="Settings · Plantain",
    on_load=DashboardState.refresh_agent,
)

__all__ = ["app"]
l__ = ["app"]
