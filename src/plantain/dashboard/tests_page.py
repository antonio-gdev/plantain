"""Responsive Tests catalog for the local Plantain dashboard."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.scenario_catalog import MAX_SCENARIO_SEARCH_QUERY_LENGTH
from plantain.dashboard.scenario_detail_panel import scenario_detail_drawer
from plantain.dashboard.scenario_state import (
    MAX_TEST_FILTER_INPUT_LENGTH,
    ScenarioCatalogState,
)
from plantain.dashboard.shell import action_link, component, page_shell
from plantain.dashboard.theme import (
    BRAND_BLUE,
    BRAND_YELLOW,
    COMPACT_PANEL_PADDING,
    FOCUS_STYLE,
    MONO_FONT_FAMILY,
    MUTED_TEXT_COLOR,
    PANEL_PADDING,
    PANEL_STYLE,
    SUBTLE_BACKGROUND,
)


def _filter_field(
    label: str,
    value: Any,
    on_change: Any,
    placeholder: str,
) -> rx.Component:
    return component(
        rx.vstack(
            rx.text(
                label,
                color="var(--gray-11)",
                font_size="0.7rem",
                font_weight="650",
            ),
            rx.input(
                value=value,
                on_change=on_change,
                placeholder=placeholder,
                aria_label=label,
                max_length=MAX_TEST_FILTER_INPUT_LENGTH,
                size="2",
                width="100%",
                **FOCUS_STYLE,
            ),
            align="stretch",
            spacing="1",
            width="100%",
        )
    )


def _catalog_filter_actions() -> rx.Component:
    return component(
        rx.flex(
            rx.input(
                value=ScenarioCatalogState.test_filter_query,
                on_change=ScenarioCatalogState.change_test_filter_query,
                placeholder="Search tests",
                aria_label="Search tests",
                max_length=MAX_SCENARIO_SEARCH_QUERY_LENGTH,
                width="100%",
                **FOCUS_STYLE,
            ),
            rx.button(
                "Apply",
                on_click=ScenarioCatalogState.apply_test_filters,
                loading=ScenarioCatalogState.test_catalog_loading,
                **FOCUS_STYLE,
            ),
            rx.button(
                "Clear",
                on_click=ScenarioCatalogState.clear_test_filters,
                variant="soft",
                **FOCUS_STYLE,
            ),
            align={"initial": "stretch", "sm": "center"},
            direction={"initial": "column", "sm": "row"},
            gap="2",
            width="100%",
        )
    )


def _catalog_filters() -> rx.Component:
    return component(
        rx.vstack(
            _catalog_filter_actions(),
            rx.grid(
                _filter_field(
                    "Require every tag",
                    ScenarioCatalogState.test_filter_required_all,
                    ScenarioCatalogState.change_test_filter_required_all,
                    "smoke, checkout",
                ),
                _filter_field(
                    "Match any tag",
                    ScenarioCatalogState.test_filter_required_any,
                    ScenarioCatalogState.change_test_filter_required_any,
                    "api, ui, database",
                ),
                _filter_field(
                    "Exclude tags",
                    ScenarioCatalogState.test_filter_excluded,
                    ScenarioCatalogState.change_test_filter_excluded,
                    "slow, destructive",
                ),
                columns={"initial": "1", "md": "3"},
                gap="3",
                width="100%",
            ),
            spacing="3",
            width="100%",
        )
    )


def _status_badge(item: Any) -> rx.Component:
    return component(
        rx.cond(
            item["status"] == "ready",
            rx.hstack(
                rx.icon("circle-check", color="var(--green-9)", size=14),
                rx.text(
                    item["status_label"],
                    color=MUTED_TEXT_COLOR,
                    font_size="0.72rem",
                    font_weight="650",
                ),
                align="center",
                spacing="1",
            ),
            rx.hstack(
                rx.icon("circle-alert", color="var(--yellow-10)", size=14),
                rx.text(
                    item["status_label"],
                    color="var(--yellow-11)",
                    font_size="0.72rem",
                    font_weight="650",
                ),
                align="center",
                spacing="1",
            ),
        )
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
                    rx.icon("play", size=15),
                ),
                rx.cond(active, "In progress", "Run test"),
                aria_label=item["name"] + " — run test",
                color_scheme="blue",
                disabled=active | ScenarioCatalogState.test_catalog_loading,
                on_click=cast("Any", ScenarioCatalogState.run_test)(
                    item["scenario_id"],
                    item["name"],
                ),
                size="2",
                **FOCUS_STYLE,
            ),
        )
    )


def _selection_button(item: Any) -> rx.Component:
    selected_ids = cast("Any", ScenarioCatalogState.test_selected_ids)
    selected = selected_ids.contains(item["scenario_id"])
    return component(
        rx.icon_button(
            rx.cond(
                selected,
                rx.icon("circle-check", size=14),
                rx.icon("circle", size=14),
            ),
            aria_label=rx.cond(
                selected,
                item["name"] + " — deselect test",
                item["name"] + " — select test",
            ),
            aria_pressed=selected,
            background=rx.cond(selected, "var(--yellow-4)", "transparent"),
            border_radius="999px",
            color_scheme="yellow",
            disabled=ScenarioCatalogState.test_catalog_loading,
            flex_shrink="0",
            on_click=cast("Any", ScenarioCatalogState.toggle_test_selection)(item["scenario_id"]),
            size="2",
            title=rx.cond(selected, "Deselect test", "Select test"),
            variant="ghost",
            **FOCUS_STYLE,
        )
    )


def _inspect_button(item: Any) -> rx.Component:
    return component(
        rx.button(
            rx.icon("list-tree", size=15),
            "View steps",
            aria_label="Inspect test steps",
            color_scheme="gray",
            disabled=ScenarioCatalogState.test_catalog_loading,
            on_click=cast(
                "Any",
                ScenarioCatalogState.open_test_detail,
            )(item["scenario_id"]),
            size="2",
            variant="soft",
            **FOCUS_STYLE,
        )
    )


def _live_progress(item: Any) -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.text(
                    item["phase"],
                    color="var(--gray-12)",
                    font_size="0.73rem",
                    font_weight="650",
                ),
                rx.spacer(),
                rx.text(
                    item["progress_label"],
                    color="var(--gray-10)",
                    font_size="0.7rem",
                ),
                align="center",
                width="100%",
            ),
            rx.box(
                rx.box(
                    background=BRAND_BLUE,
                    border_radius="inherit",
                    height="100%",
                    transition="width 180ms ease",
                    width=item["progress_width"],
                ),
                background="var(--gray-a4)",
                border_radius="999px",
                height="0.38rem",
                overflow="hidden",
                role="progressbar",
                aria_label=item["name"] + " execution progress",
                aria_valuemin=0,
                aria_valuemax=100,
                aria_valuenow=item["progress_percent"],
                aria_valuetext=item["progress_label"],
                width="100%",
            ),
            rx.hstack(
                rx.cond(
                    item["activity"] != "",
                    rx.badge(item["activity"], color_scheme="blue", variant="soft"),
                ),
                rx.cond(
                    item["step_id"] != "",
                    rx.badge(item["step_id"], color_scheme="gray", variant="outline"),
                ),
                rx.badge(item["elapsed"], color_scheme="gray", variant="soft"),
                align="center",
                spacing="2",
                wrap="wrap",
            ),
            spacing="2",
            width="100%",
        )
    )


def _live_evidence(item: Any) -> rx.Component:
    return component(
        rx.box(
            rx.hstack(
                rx.icon("radio", color=BRAND_YELLOW, size=14),
                rx.text(
                    "Live execution evidence",
                    font_size="0.7rem",
                    font_weight="650",
                ),
                align="center",
                margin_bottom="0.35rem",
                spacing="2",
            ),
            rx.text(
                item["timeline"],
                color="var(--gray-10)",
                font_family=MONO_FONT_FAMILY,
                font_size="0.66rem",
                line_height="1.55",
                white_space="pre-line",
            ),
            background="var(--gray-a2)",
            border="1px solid var(--gray-a4)",
            border_radius="0.65rem",
            padding="0.65rem 0.72rem",
            width="100%",
        )
    )


def _active_run_card(item: Any) -> rx.Component:
    return component(
        rx.flex(
            rx.hstack(
                rx.spinner(size="2"),
                rx.vstack(
                    rx.hstack(
                        rx.text(
                            item["name"],
                            font_size="0.84rem",
                            font_weight="700",
                        ),
                        rx.badge(
                            item["status_label"],
                            color_scheme="yellow",
                            variant="soft",
                        ),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.text(
                        item["message"],
                        color="var(--gray-10)",
                        font_size="0.74rem",
                        role="status",
                    ),
                    _live_progress(item),
                    _live_evidence(item),
                    align="start",
                    min_width="0",
                    spacing="1",
                    width="100%",
                ),
                align="center",
                flex="1",
                min_width="0",
                spacing="3",
                width="100%",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("square", size=13),
                "Stop",
                aria_label=item["name"] + " — stop test run",
                color_scheme="red",
                disabled=item["job_id"] == "",
                on_click=cast("Any", ScenarioCatalogState.cancel_test)(item["job_id"]),
                size="1",
                variant="outline",
                **FOCUS_STYLE,
            ),
            align={"initial": "stretch", "sm": "center"},
            direction={"initial": "column", "sm": "row"},
            gap="3",
            padding="0.85rem",
            width="100%",
            **PANEL_STYLE,
        )
    )


def _run_result_badge(item: Any) -> rx.Component:
    return component(
        rx.cond(
            item["status"] == "passed",
            rx.badge(
                rx.icon("circle-check", size=13),
                item["status_label"],
                color_scheme="blue",
                variant="soft",
            ),
            rx.cond(
                item["status"] == "failed",
                rx.badge(
                    rx.icon("circle-alert", size=13),
                    item["status_label"],
                    color_scheme="red",
                    variant="soft",
                ),
                rx.badge(
                    rx.icon("circle-pause", size=13),
                    item["status_label"],
                    color_scheme="yellow",
                    variant="soft",
                ),
            ),
        )
    )


def _rerun_button(item: Any) -> rx.Component:
    active_ids = cast("Any", ScenarioCatalogState.test_active_run_ids)
    active = active_ids.contains(item["scenario_id"])
    return component(
        rx.button(
            rx.cond(
                active,
                rx.spinner(size="1"),
                rx.icon("rotate-ccw", size=13),
            ),
            rx.cond(active, "In progress", "Run again"),
            aria_label=item["name"] + " — run test again",
            disabled=active,
            on_click=cast("Any", ScenarioCatalogState.run_test)(
                item["scenario_id"],
                item["name"],
            ),
            size="1",
            variant="soft",
            **FOCUS_STYLE,
        )
    )


def _run_result_card(item: Any) -> rx.Component:
    return component(
        rx.flex(
            rx.vstack(
                rx.hstack(
                    rx.text(
                        item["name"],
                        font_size="0.84rem",
                        font_weight="700",
                    ),
                    _run_result_badge(item),
                    _rerun_button(item),
                    align="center",
                    spacing="2",
                    wrap="wrap",
                ),
                rx.text(
                    item["message"],
                    color="var(--gray-10)",
                    font_size="0.74rem",
                ),
                rx.hstack(
                    rx.cond(
                        item["duration"] != "",
                        rx.badge(
                            item["duration"],
                            color_scheme="gray",
                            variant="soft",
                        ),
                    ),
                    rx.cond(
                        item["steps"] != "",
                        rx.badge(
                            item["steps"],
                            color_scheme="gray",
                            variant="soft",
                        ),
                    ),
                    align="center",
                    spacing="2",
                    wrap="wrap",
                ),
                rx.cond(
                    item["correlation_id"] != "",
                    rx.text(
                        "Run ",
                        item["correlation_id"],
                        color="var(--gray-9)",
                        font_family=MONO_FONT_FAMILY,
                        font_size="0.68rem",
                    ),
                ),
                rx.cond(
                    (item["database_workflow_id"] != "")
                    & (item["database_workflow_id"] == ScenarioCatalogState.database_workflow_id)
                    & (item["scenario_id"] == ScenarioCatalogState.database_workflow_scenario_id),
                    rx.button(
                        rx.icon("arrow-right", size=14),
                        "Continue discovery",
                        aria_label="Continue guided database discovery",
                        color_scheme="blue",
                        on_click=[
                            cast("Any", ScenarioCatalogState.continue_database_discovery)(
                                item["database_workflow_id"],
                                item["scenario_id"],
                                item["correlation_id"],
                            ),
                            rx.redirect("/create"),
                        ],
                        size="2",
                        variant="soft",
                        **FOCUS_STYLE,
                    ),
                ),
                rx.cond(
                    (item["ui_workflow_id"] != "")
                    & (item["ui_workflow_id"] == ScenarioCatalogState.ui_workflow_id)
                    & (item["scenario_id"] == ScenarioCatalogState.ui_workflow_scenario_id),
                    rx.button(
                        rx.cond(
                            item["status"] == "failed",
                            rx.icon("wrench", size=14),
                            rx.icon("arrow-right", size=14),
                        ),
                        rx.cond(
                            item["status"] == "failed",
                            "Repair locator",
                            "Continue discovery",
                        ),
                        aria_label="Continue guided UI discovery or repair",
                        color_scheme=rx.cond(
                            item["status"] == "failed",
                            "yellow",
                            "blue",
                        ),
                        on_click=[
                            cast("Any", ScenarioCatalogState.continue_ui_discovery)(
                                item["ui_workflow_id"],
                                item["scenario_id"],
                                item["correlation_id"],
                            ),
                            rx.redirect("/create"),
                        ],
                        size="2",
                        variant="soft",
                        **FOCUS_STYLE,
                    ),
                ),
                align="start",
                min_width="0",
                spacing="2",
            ),
            align="center",
            padding="0.85rem",
            width="100%",
            **PANEL_STYLE,
        )
    )


def run_activity() -> rx.Component:
    return component(
        rx.vstack(
            rx.cond(
                ScenarioCatalogState.test_run_notice != "",
                rx.callout(
                    ScenarioCatalogState.test_run_notice,
                    icon="info",
                    color_scheme="yellow",
                    role="status",
                    width="100%",
                ),
            ),
            rx.cond(
                ScenarioCatalogState.test_active_run_count > 0,
                rx.vstack(
                    rx.hstack(
                        rx.icon("activity", size=18, color=BRAND_YELLOW),
                        rx.heading("Active runs", size="4"),
                        rx.badge(
                            ScenarioCatalogState.test_active_run_count,
                            color_scheme="yellow",
                            variant="soft",
                        ),
                        align="center",
                        spacing="2",
                    ),
                    rx.text(
                        "Tests execute concurrently while Plantain applies shared "
                        "resource limits automatically.",
                        color="var(--gray-10)",
                        font_size="0.78rem",
                    ),
                    rx.foreach(
                        ScenarioCatalogState.test_active_runs,
                        _active_run_card,
                    ),
                    align="stretch",
                    spacing="3",
                    width="100%",
                ),
            ),
            rx.cond(
                ScenarioCatalogState.test_run_result_count > 0,
                rx.vstack(
                    rx.hstack(
                        rx.icon("history", size=18, color=BRAND_BLUE),
                        rx.heading("Latest session results", size="4"),
                        rx.badge(
                            ScenarioCatalogState.test_run_result_count,
                            color_scheme="blue",
                            variant="soft",
                        ),
                        align="center",
                        spacing="2",
                    ),
                    rx.foreach(
                        ScenarioCatalogState.test_run_results,
                        _run_result_card,
                    ),
                    align="stretch",
                    spacing="3",
                    width="100%",
                ),
            ),
            align="stretch",
            spacing="3",
            width="100%",
        )
    )


def _selection_summary() -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.icon(
                    "list-checks",
                    size=18,
                    color=rx.cond(
                        ScenarioCatalogState.test_selection_mode,
                        BRAND_YELLOW,
                        MUTED_TEXT_COLOR,
                    ),
                ),
                rx.heading(
                    rx.cond(
                        ScenarioCatalogState.test_selection_mode,
                        "Select tests",
                        "Test scope",
                    ),
                    size="4",
                ),
                rx.cond(
                    ScenarioCatalogState.test_selection_mode,
                    rx.badge(
                        ScenarioCatalogState.test_selected_count,
                        color_scheme="yellow",
                        variant="soft",
                    ),
                ),
                rx.cond(
                    ScenarioCatalogState.test_batch_active,
                    rx.badge(
                        ScenarioCatalogState.test_batch_completed,
                        " / ",
                        ScenarioCatalogState.test_batch_total,
                        " finished",
                        color_scheme="yellow",
                        variant="soft",
                    ),
                ),
                align="center",
                spacing="2",
            ),
            rx.text(
                rx.cond(
                    ScenarioCatalogState.test_batch_active,
                    "Batch scope is locked while queued tests run.",
                    rx.cond(
                        ScenarioCatalogState.test_selection_mode,
                        rx.cond(
                            ScenarioCatalogState.test_selected_count > 0,
                            "Actions apply only to the selected tests.",
                            "Use the circles beside test names to build a selection.",
                        ),
                        "Actions apply to all current matches.",
                    ),
                ),
                color="var(--gray-10)",
                font_size="0.76rem",
            ),
            align="start",
            spacing="1",
        )
    )


def _selection_controls() -> rx.Component:
    return component(
        rx.flex(
            rx.cond(
                ScenarioCatalogState.test_selection_mode,
                rx.button(
                    "Select visible",
                    color_scheme="yellow",
                    disabled=(
                        ScenarioCatalogState.test_batch_active
                        | ScenarioCatalogState.test_catalog_loading
                    ),
                    on_click=ScenarioCatalogState.select_test_page,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                rx.button(
                    rx.icon("list-checks", size=14),
                    "Select tests",
                    color_scheme="gray",
                    disabled=(
                        (ScenarioCatalogState.test_catalog_total == 0)
                        | ScenarioCatalogState.test_batch_active
                        | ScenarioCatalogState.test_catalog_loading
                    ),
                    on_click=ScenarioCatalogState.begin_test_selection,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
            ),
            rx.cond(
                ScenarioCatalogState.test_selection_mode,
                rx.button(
                    "Clear",
                    color_scheme="gray",
                    disabled=(
                        (ScenarioCatalogState.test_selected_count == 0)
                        | ScenarioCatalogState.test_batch_active
                    ),
                    on_click=ScenarioCatalogState.clear_test_selection,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
            ),
            rx.cond(
                ScenarioCatalogState.test_selection_mode,
                rx.button(
                    "Done",
                    color_scheme="gray",
                    disabled=ScenarioCatalogState.test_batch_active,
                    on_click=ScenarioCatalogState.end_test_selection,
                    size="2",
                    variant="ghost",
                    **FOCUS_STYLE,
                ),
            ),
            rx.button(
                rx.icon("shield-check", size=15),
                rx.cond(
                    ScenarioCatalogState.test_selection_mode,
                    "Validate selected",
                    "Validate matching",
                ),
                color_scheme="gray",
                disabled=(
                    (ScenarioCatalogState.test_catalog_total == 0)
                    | ScenarioCatalogState.test_batch_active
                    | (
                        ScenarioCatalogState.test_selection_mode
                        & (ScenarioCatalogState.test_selected_count == 0)
                    )
                ),
                loading=ScenarioCatalogState.test_validation_loading,
                on_click=ScenarioCatalogState.validate_test_selection,
                size="2",
                variant="soft",
                **FOCUS_STYLE,
            ),
            rx.cond(
                ScenarioCatalogState.test_batch_active,
                rx.button(
                    rx.icon("square", size=14),
                    "Stop batch",
                    aria_label="Stop the active test batch",
                    color_scheme="red",
                    on_click=cast("Any", ScenarioCatalogState.cancel_test_batch)(
                        ScenarioCatalogState.test_batch_id
                    ),
                    size="2",
                    variant="outline",
                ),
                rx.button(
                    rx.icon("play", size=14),
                    rx.cond(
                        ScenarioCatalogState.test_selection_mode,
                        "Run selected",
                        "Run matching",
                    ),
                    color_scheme="blue",
                    disabled=(
                        (ScenarioCatalogState.test_catalog_total == 0)
                        | ScenarioCatalogState.test_catalog_loading
                        | ScenarioCatalogState.test_validation_loading
                        | (
                            ScenarioCatalogState.test_selection_mode
                            & (ScenarioCatalogState.test_selected_count == 0)
                        )
                    ),
                    on_click=ScenarioCatalogState.run_test_selection,
                    size="2",
                ),
            ),
            gap="2",
            wrap="wrap",
        )
    )


def _selection_actions() -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                _selection_summary(),
                rx.spacer(),
                _selection_controls(),
                align={"initial": "stretch", "md": "center"},
                direction={"initial": "column", "md": "row"},
                gap="3",
                width="100%",
            ),
            rx.cond(
                ScenarioCatalogState.test_validation_notice != "",
                rx.callout(
                    ScenarioCatalogState.test_validation_notice,
                    icon="info",
                    color_scheme="blue",
                    role="status",
                    width="100%",
                ),
            ),
            spacing="3",
            width="100%",
        )
    )


def _scope_toolbar() -> rx.Component:
    return component(
        rx.vstack(
            _catalog_filters(),
            _selection_actions(),
            align="stretch",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        )
    )


def _test_card(item: Any) -> rx.Component:
    selected_ids = cast("Any", ScenarioCatalogState.test_selected_ids)
    selected = selected_ids.contains(item["scenario_id"])
    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.hstack(
                        rx.cond(
                            ScenarioCatalogState.test_selection_mode,
                            _selection_button(item),
                        ),
                        rx.heading(item["name"], size="4"),
                        _status_badge(item),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
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
                    _inspect_button(item),
                    _run_button(item),
                    align="center",
                    spacing="2",
                ),
                align="start",
                direction={"initial": "column", "sm": "row"},
                gap="3",
                width="100%",
            ),
            rx.hstack(
                rx.badge(item["domains"], color_scheme="gray", variant="outline"),
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
            border_radius="10px",
            outline=rx.cond(
                ScenarioCatalogState.test_selection_mode & selected,
                f"2px solid {BRAND_YELLOW}",
                "2px solid transparent",
            ),
            outline_offset="-2px",
            padding=COMPACT_PANEL_PADDING,
            spacing="3",
            width="100%",
        )
    )


def _empty_catalog() -> rx.Component:
    return component(
        rx.center(
            rx.vstack(
                rx.box(
                    rx.icon("list-checks", size=25, color=BRAND_BLUE),
                    background="var(--blue-3)",
                    border_radius="12px",
                    padding="0.8rem",
                ),
                rx.heading("No tests yet", size="5"),
                rx.text(
                    "Describe the outcome you want to test. Plantain will choose "
                    "the right workflow and keep the resulting YAML reviewable.",
                    color="var(--gray-10)",
                    max_width="520px",
                    text_align="center",
                ),
                action_link(
                    "Create a test",
                    icon="plus",
                    href="/create",
                ),
                align="center",
                spacing="3",
            ),
            min_height="300px",
            padding="2rem",
            width="100%",
            **PANEL_STYLE,
        )
    )


def _catalog_feedback() -> rx.Component:
    return component(
        rx.vstack(
            rx.cond(
                ScenarioCatalogState.test_catalog_error != "",
                rx.callout(
                    ScenarioCatalogState.test_catalog_error,
                    icon="circle-alert",
                    color_scheme="red",
                    role="alert",
                    width="100%",
                ),
            ),
            rx.cond(
                ScenarioCatalogState.test_catalog_notice != "",
                rx.callout(
                    ScenarioCatalogState.test_catalog_notice,
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


def _catalog_header() -> rx.Component:
    return component(
        rx.desktop_only(
            rx.flex(
                rx.text(
                    "Test",
                    color=MUTED_TEXT_COLOR,
                    font_size="0.7rem",
                    font_weight="700",
                    text_transform="uppercase",
                ),
                rx.spacer(),
                rx.text(
                    "Details · actions",
                    color=MUTED_TEXT_COLOR,
                    font_size="0.7rem",
                    font_weight="700",
                    text_transform="uppercase",
                ),
                padding="0.45rem 0.75rem",
                width="100%",
            )
        )
    )


def _catalog_content() -> rx.Component:
    return component(
        rx.cond(
            ScenarioCatalogState.test_catalog_loading,
            rx.center(
                rx.vstack(
                    rx.spinner(size="3"),
                    rx.text(
                        "Checking local tests…",
                        color="var(--gray-10)",
                        font_size="0.82rem",
                    ),
                    align="center",
                    spacing="2",
                ),
                min_height="280px",
                role="status",
                width="100%",
            ),
            rx.cond(
                ScenarioCatalogState.test_catalog_total == 0,
                _empty_catalog(),
                rx.vstack(
                    _catalog_header(),
                    rx.foreach(
                        ScenarioCatalogState.test_catalog_items,
                        _test_card,
                    ),
                    align="stretch",
                    padding="0.45rem",
                    spacing="1",
                    width="100%",
                    **PANEL_STYLE,
                ),
            ),
        )
    )


def _pagination() -> rx.Component:
    return component(
        rx.cond(
            ScenarioCatalogState.test_catalog_total > 0,
            rx.hstack(
                rx.button(
                    rx.icon("chevron-left", size=16),
                    "Previous",
                    aria_label="Previous tests page",
                    disabled=(
                        ~ScenarioCatalogState.test_catalog_has_previous
                        | ScenarioCatalogState.test_catalog_loading
                    ),
                    on_click=ScenarioCatalogState.previous_test_page,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                rx.spacer(),
                rx.text(
                    "Page ",
                    ScenarioCatalogState.test_catalog_page,
                    " of ",
                    rx.cond(
                        ScenarioCatalogState.test_catalog_page_count > 0,
                        ScenarioCatalogState.test_catalog_page_count,
                        1,
                    ),
                    color="var(--gray-10)",
                    font_size="0.78rem",
                    role="status",
                ),
                rx.spacer(),
                rx.button(
                    "Next",
                    rx.icon("chevron-right", size=16),
                    aria_label="Next tests page",
                    disabled=(
                        ~ScenarioCatalogState.test_catalog_has_next
                        | ScenarioCatalogState.test_catalog_loading
                    ),
                    on_click=ScenarioCatalogState.next_test_page,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                align="center",
                padding_top="0.25rem",
                width="100%",
            ),
        )
    )


def tests_page() -> rx.Component:
    """Render real reviewable scenarios from the local workspace."""

    content = rx.vstack(
        rx.flex(
            rx.vstack(
                rx.hstack(
                    rx.heading("TESTS", size="4", letter_spacing="-0.025em"),
                    rx.badge(
                        ScenarioCatalogState.test_catalog_total,
                        color_scheme="gray",
                        size="2",
                        variant="soft",
                    ),
                    align="center",
                    spacing="2",
                ),
                align="start",
            ),
            rx.spacer(),
            action_link(
                "Create a test",
                icon="plus",
                href="/create",
            ),
            align={"initial": "stretch", "sm": "end"},
            direction={"initial": "column", "sm": "row"},
            gap="3",
            width="100%",
        ),
        _scope_toolbar(),
        _catalog_feedback(),
        run_activity(),
        _catalog_content(),
        _pagination(),
        scenario_detail_drawer(),
        align="stretch",
        max_width="1060px",
        spacing="5",
        width="100%",
    )
    return page_shell(
        component(content),
        active="tests",
        title="Tests",
    )


__all__ = ["run_activity", "tests_page"]
