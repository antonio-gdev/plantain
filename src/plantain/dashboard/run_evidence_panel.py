"""Progressively disclosed immutable run-evidence inspector."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.run_history_panel import run_status_badge
from plantain.dashboard.run_state import RunCatalogState
from plantain.dashboard.shell import component
from plantain.dashboard.theme import (
    BORDER_COLOR,
    BRAND_BLUE,
    BRAND_RED,
    BRAND_YELLOW,
    FOCUS_STYLE,
    MONO_FONT_FAMILY,
    PANEL_BACKGROUND,
    PANEL_PADDING,
    SUBTLE_BACKGROUND,
)


def _tab_button(
    label: str,
    value: str,
    icon: str,
    color_scheme: str,
) -> rx.Component:
    handler = cast("Any", RunCatalogState.show_run_evidence_tab)
    common = {
        "aria_label": f"Show {label.lower()} evidence",
        "color_scheme": cast("Any", color_scheme),
        "disabled": RunCatalogState.history_evidence_loading,
        "on_click": handler(value),
        "size": "2",
        **FOCUS_STYLE,
    }
    content = (rx.icon(icon, size=14), label)
    return component(
        rx.cond(
            RunCatalogState.history_evidence_tab == value,
            rx.button(*content, aria_pressed=True, variant="solid", **common),
            rx.button(*content, aria_pressed=False, variant="soft", **common),
        )
    )


def _inspector_header() -> rx.Component:
    return component(
        rx.flex(
            rx.vstack(
                rx.hstack(
                    rx.drawer.title(
                        "Run evidence",
                        font_size="1rem",
                        font_weight="750",
                    ),
                    rx.badge(
                        "On demand",
                        color_scheme="blue",
                        variant="soft",
                    ),
                    align="center",
                    spacing="2",
                    wrap="wrap",
                ),
                rx.drawer.description(
                    "Sanitized local execution evidence.",
                    color="var(--gray-10)",
                    font_size="0.78rem",
                ),
                align="start",
                spacing="1",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("x", size=14),
                "Close",
                aria_label="Close run evidence",
                color_scheme="gray",
                on_click=RunCatalogState.close_run_evidence,
                variant="soft",
                **FOCUS_STYLE,
            ),
            align={"initial": "stretch", "sm": "center"},
            direction={"initial": "column", "sm": "row"},
            gap="3",
            width="100%",
        )
    )


def _tab_bar() -> rx.Component:
    return component(
        rx.flex(
            _tab_button("Steps", "steps", "list-checks", "blue"),
            _tab_button("Operations", "operations", "activity", "blue"),
            _tab_button("Artifacts", "artifacts", "paperclip", "yellow"),
            _tab_button("Failure / info", "failure", "triangle-alert", "red"),
            align="center",
            aria_label="Evidence categories",
            gap="2",
            role="group",
            wrap="wrap",
            width="100%",
        )
    )


def _step_card(item: Any) -> rx.Component:
    return component(
        rx.flex(
            rx.center(
                rx.text(
                    item["position"],
                    color="white",
                    font_size="0.7rem",
                    font_weight="750",
                ),
                background=BRAND_BLUE,
                border_radius="999px",
                flex_shrink="0",
                height="28px",
                width="28px",
            ),
            rx.vstack(
                rx.text(item["title"], font_size="0.82rem", font_weight="700"),
                rx.text(
                    item["subtitle"],
                    color="var(--gray-9)",
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.68rem",
                    word_break="break-word",
                ),
                align="start",
                min_width="0",
                spacing="1",
            ),
            rx.spacer(),
            rx.vstack(
                run_status_badge(item["status"], item["status_label"]),
                rx.text(
                    item["duration"],
                    color="var(--gray-9)",
                    font_size="0.68rem",
                ),
                align="end",
                spacing="1",
            ),
            align="center",
            background=SUBTLE_BACKGROUND,
            border_radius="11px",
            gap="3",
            padding="0.8rem",
            width="100%",
        )
    )


def _evidence_value(
    label: Any,
    value: Any,
    limited: Any,
) -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.text(
                    label,
                    color="var(--gray-10)",
                    font_size="0.65rem",
                    font_weight="750",
                    letter_spacing="0.06em",
                    text_transform="uppercase",
                ),
                rx.cond(
                    limited,
                    rx.badge(
                        "Display truncated",
                        color_scheme="yellow",
                        size="1",
                        variant="soft",
                    ),
                ),
                align="center",
                spacing="2",
                wrap="wrap",
            ),
            rx.box(
                rx.text(
                    value,
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.68rem",
                    line_height="1.5",
                    white_space="pre-wrap",
                    word_break="break-word",
                ),
                background=SUBTLE_BACKGROUND,
                border_radius="9px",
                max_height="260px",
                overflow="auto",
                padding="0.7rem",
                width="100%",
            ),
            align="stretch",
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
                        item["position"],
                        color_scheme="gray",
                        variant="soft",
                    ),
                    rx.text(item["title"], font_size="0.82rem", font_weight="750"),
                    rx.text(
                        item["subtitle"],
                        color="var(--gray-9)",
                        font_size="0.7rem",
                    ),
                    align="center",
                    spacing="2",
                    wrap="wrap",
                ),
                rx.spacer(),
                rx.hstack(
                    run_status_badge(item["status"], item["status_label"]),
                    rx.text(
                        item["duration"],
                        color="var(--gray-9)",
                        font_size="0.68rem",
                    ),
                    align="center",
                    spacing="2",
                ),
                align={"initial": "start", "sm": "center"},
                direction={"initial": "column", "sm": "row"},
                gap="2",
                width="100%",
            ),
            rx.text(
                item["activity"],
                " · ",
                item["step_id"],
                color="var(--gray-10)",
                font_size="0.7rem",
            ),
            rx.box(
                rx.text(
                    item["target"],
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.68rem",
                    word_break="break-word",
                ),
                background="var(--gray-3)",
                border_radius="8px",
                padding="0.55rem 0.65rem",
                width="100%",
            ),
            rx.cond(
                item["error_type"] != "",
                rx.badge(
                    item["error_type"],
                    color_scheme="red",
                    variant="soft",
                ),
            ),
            rx.grid(
                rx.cond(
                    item["input"] != "",
                    _evidence_value(
                        "Input",
                        item["input"],
                        item["input_limited"] == "true",
                    ),
                ),
                rx.cond(
                    item["expected"] != "",
                    _evidence_value(
                        "Expected",
                        item["expected"],
                        item["expected_limited"] == "true",
                    ),
                ),
                rx.cond(
                    item["actual"] != "",
                    _evidence_value(
                        "Actual",
                        item["actual"],
                        item["actual_limited"] == "true",
                    ),
                ),
                columns={"initial": "1", "xl": "3"},
                gap="3",
                width="100%",
            ),
            align="stretch",
            background=SUBTLE_BACKGROUND,
            border_radius="11px",
            padding="0.9rem",
            spacing="3",
            width="100%",
        )
    )


def _artifact_card(item: Any) -> rx.Component:
    return component(
        rx.flex(
            rx.box(
                rx.icon("file-text", color=BRAND_YELLOW, size=18),
                background="var(--yellow-3)",
                border_radius="9px",
                padding="0.65rem",
            ),
            rx.vstack(
                rx.hstack(
                    rx.badge(item["position"], color_scheme="gray", variant="soft"),
                    rx.text(item["title"], font_size="0.8rem", font_weight="700"),
                    align="center",
                    spacing="2",
                    wrap="wrap",
                ),
                rx.text(
                    item["path"],
                    color="var(--gray-11)",
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.68rem",
                    word_break="break-word",
                ),
                rx.text(
                    item["description"],
                    color="var(--gray-9)",
                    font_size="0.72rem",
                ),
                align="start",
                min_width="0",
                spacing="1",
            ),
            align="start",
            background=SUBTLE_BACKGROUND,
            border_radius="11px",
            gap="3",
            padding="0.85rem",
            width="100%",
        )
    )


def _failure_detail(item: Any) -> rx.Component:
    return _evidence_value(
        item["label"],
        item["value"],
        item["limited"] == "true",
    )


def _failure_content() -> rx.Component:
    return component(
        rx.cond(
            RunCatalogState.history_evidence_failure_present,
            rx.vstack(
                rx.flex(
                    rx.vstack(
                        rx.text(
                            RunCatalogState.history_evidence_failure_type,
                            color="var(--red-11)",
                            font_size="0.9rem",
                            font_weight="750",
                        ),
                        rx.text(
                            RunCatalogState.history_evidence_failure_activity,
                            " · ",
                            RunCatalogState.history_evidence_failure_step_id,
                            color="var(--gray-10)",
                            font_size="0.72rem",
                        ),
                        align="start",
                        spacing="1",
                    ),
                    rx.spacer(),
                    rx.badge(
                        rx.icon("circle-alert", size=13),
                        "Failure evidence",
                        color_scheme="red",
                        variant="soft",
                    ),
                    align={"initial": "start", "sm": "center"},
                    direction={"initial": "column", "sm": "row"},
                    gap="2",
                    width="100%",
                ),
                _evidence_value(
                    "Message",
                    RunCatalogState.history_evidence_failure_message,
                    RunCatalogState.history_evidence_failure_message_limited,
                ),
                rx.foreach(
                    RunCatalogState.history_evidence_failure_details,
                    _failure_detail,
                ),
                align="stretch",
                background="var(--red-3)",
                border=f"1px solid {BRAND_RED}",
                border_radius="11px",
                padding="1rem",
                spacing="3",
                width="100%",
            ),
            rx.center(
                rx.vstack(
                    rx.icon("shield-check", color=BRAND_BLUE, size=24),
                    rx.heading("No failure evidence", size="4"),
                    rx.text(
                        "This run did not record a structured failure payload.",
                        color="var(--gray-10)",
                        text_align="center",
                    ),
                    align="center",
                    spacing="2",
                ),
                min_height="180px",
                width="100%",
            ),
        )
    )


def _empty_content() -> rx.Component:
    return component(
        rx.center(
            rx.vstack(
                rx.icon("inbox", color="var(--gray-9)", size=24),
                rx.heading("No evidence in this category", size="4"),
                rx.text(
                    "The immutable result did not record any ",
                    RunCatalogState.history_evidence_tab,
                    " evidence.",
                    color="var(--gray-10)",
                    text_align="center",
                ),
                align="center",
                spacing="2",
            ),
            min_height="180px",
            width="100%",
        )
    )


def _loaded_content() -> rx.Component:
    collection = rx.cond(
        RunCatalogState.history_evidence_tab == "operations",
        rx.vstack(
            rx.foreach(RunCatalogState.history_evidence_items, _operation_card),
            align="stretch",
            spacing="3",
            width="100%",
        ),
        rx.cond(
            RunCatalogState.history_evidence_tab == "artifacts",
            rx.vstack(
                rx.foreach(RunCatalogState.history_evidence_items, _artifact_card),
                align="stretch",
                spacing="3",
                width="100%",
            ),
            rx.vstack(
                rx.foreach(RunCatalogState.history_evidence_items, _step_card),
                align="stretch",
                spacing="2",
                width="100%",
            ),
        ),
    )
    return component(
        rx.cond(
            RunCatalogState.history_evidence_tab == "failure",
            _failure_content(),
            rx.cond(
                RunCatalogState.history_evidence_total > 0,
                collection,
                _empty_content(),
            ),
        )
    )


def _evidence_body() -> rx.Component:
    return component(
        rx.cond(
            RunCatalogState.history_evidence_loading,
            rx.center(
                rx.vstack(
                    rx.spinner(size="3"),
                    rx.text(
                        "Loading selected evidence…",
                        color="var(--gray-10)",
                        font_size="0.78rem",
                    ),
                    align="center",
                    spacing="2",
                ),
                min_height="220px",
                role="status",
                width="100%",
            ),
            rx.cond(
                RunCatalogState.history_evidence_error != "",
                rx.callout(
                    RunCatalogState.history_evidence_error,
                    icon="triangle-alert",
                    color_scheme="red",
                    role="alert",
                    width="100%",
                ),
                rx.cond(
                    RunCatalogState.history_evidence_loaded,
                    _loaded_content(),
                    _empty_content(),
                ),
            ),
        )
    )


def _pagination() -> rx.Component:
    return component(
        rx.cond(
            RunCatalogState.history_evidence_page_count > 1,
            rx.flex(
                rx.button(
                    rx.icon("chevron-left", size=14),
                    "Previous",
                    disabled=(
                        ~RunCatalogState.history_evidence_has_previous
                        | RunCatalogState.history_evidence_loading
                    ),
                    on_click=RunCatalogState.previous_run_evidence_page,
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                rx.text(
                    "Page ",
                    RunCatalogState.history_evidence_page,
                    " of ",
                    RunCatalogState.history_evidence_page_count,
                    color="var(--gray-10)",
                    font_size="0.72rem",
                ),
                rx.button(
                    "Next",
                    rx.icon("chevron-right", size=14),
                    disabled=(
                        ~RunCatalogState.history_evidence_has_next
                        | RunCatalogState.history_evidence_loading
                    ),
                    on_click=RunCatalogState.next_run_evidence_page,
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                align="center",
                role="status",
                justify="between",
                width="100%",
            ),
        )
    )


def _evidence_feedback() -> rx.Component:
    return component(
        rx.vstack(
            rx.cond(
                RunCatalogState.history_evidence_notice != "",
                rx.callout(
                    RunCatalogState.history_evidence_notice,
                    icon="info",
                    color_scheme="yellow",
                    role="status",
                    size="1",
                    width="100%",
                ),
            ),
            rx.cond(
                RunCatalogState.history_evidence_total_limited,
                rx.callout(
                    "The evidence count is a lower bound because the viewer reached "
                    "its safe display scan limit.",
                    icon="info",
                    color_scheme="yellow",
                    role="status",
                    size="1",
                    width="100%",
                ),
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


def run_evidence_panel() -> rx.Component:
    """Render the explicit, bounded evidence inspector."""

    return component(
        rx.vstack(
            _inspector_header(),
            _tab_bar(),
            _evidence_body(),
            _evidence_feedback(),
            _pagination(),
            align="stretch",
            aria_label="Run evidence",
            role="region",
            spacing="4",
            width="100%",
        )
    )


def _evidence_drawer_surface() -> rx.Component:
    return component(
        rx.drawer.portal(
            rx.drawer.overlay(
                background="rgba(15, 23, 42, 0.46)",
                inset="0",
                position="fixed",
                z_index="60",
            ),
            rx.drawer.content(
                rx.box(
                    run_evidence_panel(),
                    height="100%",
                    overflow_y="auto",
                    padding=PANEL_PADDING,
                    width="100%",
                ),
                background=PANEL_BACKGROUND,
                border_left=f"1px solid {BORDER_COLOR}",
                bottom="0",
                box_shadow="-12px 0 32px rgba(15, 23, 42, 0.18)",
                left="auto",
                max_width="920px",
                position="fixed",
                right="0",
                top="0",
                width="96vw",
                z_index="61",
            ),
        )
    )


def run_evidence_drawer() -> rx.Component:
    """Render evidence as secondary detail without shifting result layout."""

    return component(
        rx.drawer.root(
            _evidence_drawer_surface(),
            direction="right",
            modal=True,
            on_open_change=RunCatalogState.change_run_evidence_open,
            open=RunCatalogState.history_evidence_open,
        )
    )


__all__ = ["run_evidence_drawer", "run_evidence_panel"]
