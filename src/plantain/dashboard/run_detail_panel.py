"""Selected immutable-run metadata panel."""

from __future__ import annotations

from typing import Any

import reflex as rx

from plantain.dashboard.run_history_panel import run_status_badge
from plantain.dashboard.run_state import RunCatalogState
from plantain.dashboard.shell import component
from plantain.dashboard.theme import (
    BRAND_BLUE,
    BRAND_RED,
    FOCUS_STYLE,
    PANEL_PADDING,
    PANEL_STYLE,
)


def _detail_line(icon: str, label: str, value: Any) -> rx.Component:
    return component(
        rx.hstack(
            rx.box(
                rx.icon(icon, color="var(--gray-10)", size=15),
                background="var(--gray-3)",
                border_radius="8px",
                padding="0.45rem",
            ),
            rx.vstack(
                rx.text(
                    label.upper(),
                    color="var(--gray-9)",
                    font_size="0.62rem",
                    font_weight="700",
                    letter_spacing="0.08em",
                ),
                rx.text(
                    value,
                    font_size="0.78rem",
                    font_weight="580",
                    word_break="break-word",
                ),
                align="start",
                min_width="0",
                spacing="1",
            ),
            align="center",
            min_width="0",
            spacing="3",
            width="100%",
        )
    )


def _integration_row(item: Any) -> rx.Component:
    return component(
        rx.flex(
            rx.hstack(
                rx.icon("plug", color=BRAND_BLUE, size=14),
                rx.text(item["provider"], font_size="0.78rem", font_weight="650"),
                align="center",
                spacing="2",
            ),
            rx.spacer(),
            rx.badge(item["status"], color_scheme="gray", variant="soft"),
            align="center",
            background="var(--gray-3)",
            border_radius="9px",
            padding="0.65rem 0.75rem",
            width="100%",
        )
    )


def _failure_attribution() -> rx.Component:
    return component(
        rx.cond(
            RunCatalogState.history_selected_failure != "",
            rx.vstack(
                rx.hstack(
                    rx.icon("triangle-alert", color=BRAND_RED, size=16),
                    rx.text(
                        "Failure attribution",
                        font_size="0.8rem",
                        font_weight="700",
                    ),
                    align="center",
                    spacing="2",
                ),
                rx.text(
                    RunCatalogState.history_selected_failure,
                    color="var(--red-11)",
                    font_size="0.76rem",
                ),
                rx.badge(
                    RunCatalogState.history_selected_failure_type,
                    color_scheme="red",
                    variant="soft",
                ),
                align="start",
                background="var(--red-3)",
                border=f"1px solid {BRAND_RED}",
                border_radius="10px",
                padding="0.8rem",
                spacing="2",
                width="100%",
            ),
        )
    )


def _integration_summary() -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.heading("Optional integrations", size="3"),
                rx.badge(
                    RunCatalogState.history_selected_integration_count,
                    color_scheme="gray",
                    variant="soft",
                ),
                align="center",
                spacing="2",
            ),
            rx.cond(
                RunCatalogState.history_selected_integration_count > 0,
                rx.vstack(
                    rx.foreach(
                        RunCatalogState.history_selected_integrations,
                        _integration_row,
                    ),
                    rx.cond(
                        RunCatalogState.history_selected_additional_integrations > 0,
                        rx.text(
                            "+",
                            RunCatalogState.history_selected_additional_integrations,
                            " additional integration results",
                            color="var(--gray-9)",
                            font_size="0.7rem",
                        ),
                    ),
                    align="stretch",
                    spacing="2",
                    width="100%",
                ),
                rx.text(
                    "No optional publication result was recorded for this run.",
                    color="var(--gray-10)",
                    font_size="0.76rem",
                ),
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


def _selected_detail() -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.text(
                        "RUN SUMMARY",
                        color="var(--gray-9)",
                        font_size="0.68rem",
                        font_weight="700",
                        letter_spacing="0.1em",
                    ),
                    rx.heading(
                        RunCatalogState.history_selected_scenario,
                        size="5",
                        word_break="break-word",
                    ),
                    align="start",
                    min_width="0",
                    spacing="1",
                ),
                rx.spacer(),
                run_status_badge(
                    RunCatalogState.history_selected_status,
                    RunCatalogState.history_selected_status_label,
                ),
                align="start",
                gap="3",
                width="100%",
            ),
            rx.text(
                RunCatalogState.history_selected_run_id,
                color="var(--gray-9)",
                font_family="var(--default-font-family)",
                font_size="0.68rem",
                word_break="break-all",
            ),
            rx.grid(
                _detail_line(
                    "clock",
                    "Started",
                    RunCatalogState.history_selected_started,
                ),
                _detail_line(
                    "timer",
                    "Duration",
                    RunCatalogState.history_selected_duration,
                ),
                _detail_line(
                    "file-text",
                    "Source",
                    RunCatalogState.history_selected_source,
                ),
                _detail_line(
                    "list-checks",
                    "Steps",
                    RunCatalogState.history_selected_steps,
                ),
                _detail_line(
                    "tags",
                    "Tags",
                    RunCatalogState.history_selected_tags,
                ),
                columns={"initial": "1", "sm": "2", "lg": "1", "xl": "2"},
                gap="4",
                width="100%",
            ),
            _failure_attribution(),
            _integration_summary(),
            rx.grid(
                _detail_line(
                    "ticket",
                    "Jira issue",
                    RunCatalogState.history_selected_jira,
                ),
                _detail_line(
                    "link",
                    "Test case",
                    RunCatalogState.history_selected_test_case,
                ),
                _detail_line(
                    "layers",
                    "Test run",
                    RunCatalogState.history_selected_test_run,
                ),
                columns={"initial": "1", "sm": "3", "lg": "1"},
                gap="3",
                width="100%",
            ),
            rx.flex(
                rx.button(
                    rx.icon("scan-search", size=15),
                    rx.cond(
                        RunCatalogState.history_evidence_open,
                        "Evidence open",
                        "Inspect evidence",
                    ),
                    color_scheme="blue",
                    disabled=RunCatalogState.history_evidence_loading,
                    on_click=RunCatalogState.open_run_evidence,
                    white_space="nowrap",
                    width={"initial": "100%", "sm": "auto"},
                    **FOCUS_STYLE,
                ),
                align="center",
                justify="end",
                width="100%",
            ),
            align="stretch",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
        )
    )


def _empty_detail() -> rx.Component:
    return component(
        rx.center(
            rx.vstack(
                rx.box(
                    rx.icon("mouse-pointer", color=BRAND_BLUE, size=23),
                    background="var(--blue-3)",
                    border_radius="12px",
                    padding="0.8rem",
                ),
                rx.heading("Select a run", size="4"),
                rx.text(
                    "Choose a result to review its status, timing, source, "
                    "failure attribution, and optional integrations.",
                    color="var(--gray-10)",
                    max_width="320px",
                    text_align="center",
                ),
                align="center",
                spacing="3",
            ),
            min_height="360px",
            padding="2rem",
            width="100%",
        )
    )


def run_detail_panel() -> rx.Component:
    """Render selected metadata in a responsive adjacent detail surface."""

    return component(
        rx.box(
            rx.cond(
                RunCatalogState.history_detail_loading,
                rx.center(
                    rx.vstack(
                        rx.spinner(size="3"),
                        rx.text(
                            "Loading run summary…",
                            color="var(--gray-10)",
                            font_size="0.8rem",
                        ),
                        align="center",
                        spacing="2",
                    ),
                    min_height="360px",
                    role="status",
                    width="100%",
                ),
                rx.cond(
                    RunCatalogState.history_selected,
                    _selected_detail(),
                    _empty_detail(),
                ),
            ),
            position={"initial": "static", "lg": "sticky"},
            top="84px",
            width="100%",
            **PANEL_STYLE,
        )
    )


__all__ = ["run_detail_panel"]
