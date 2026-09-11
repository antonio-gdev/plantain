"""Review surface for one validated process-owned scenario draft."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.agent.models import MAX_SCENARIO_DIRECTORY_HINT_LENGTH
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
    SUBTLE_BACKGROUND,
)


def _detail(label: str, value: Any) -> rx.Component:
    return component(
        rx.vstack(
            rx.text(
                label,
                color="var(--gray-9)",
                font_size="0.68rem",
                font_weight="650",
                letter_spacing="0.04em",
                text_transform="uppercase",
            ),
            rx.text(
                value,
                color="var(--gray-12)",
                font_size="0.8rem",
                font_weight="650",
            ),
            align="start",
            spacing="1",
        )
    )


def _status() -> rx.Component:
    return component(
        rx.flex(
            rx.badge(
                rx.icon("shield-check", size=13),
                "Validated locally",
                color_scheme="blue",
                variant="soft",
            ),
            rx.cond(
                DashboardState.draft_repaired,
                rx.badge(
                    rx.icon("sparkles", size=13),
                    "Auto-repaired and revalidated",
                    color_scheme="yellow",
                    variant="soft",
                ),
            ),
            align="center",
            gap="2",
            wrap="wrap",
        )
    )


def _pagination() -> rx.Component:
    return component(
        rx.cond(
            DashboardState.draft_page_count > 1,
            rx.flex(
                rx.button(
                    rx.icon("chevron-left", size=14),
                    "Previous",
                    aria_label="Previous scenario draft page",
                    disabled=(~DashboardState.draft_has_previous | DashboardState.draft_saving),
                    on_click=DashboardState.previous_draft_page,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                rx.text(
                    "Page ",
                    DashboardState.draft_page,
                    " of ",
                    DashboardState.draft_page_count,
                    color="var(--gray-10)",
                    font_size="0.72rem",
                    role="status",
                ),
                rx.button(
                    "Next",
                    rx.icon("chevron-right", size=14),
                    aria_label="Next scenario draft page",
                    disabled=(~DashboardState.draft_has_next | DashboardState.draft_saving),
                    on_click=DashboardState.next_draft_page,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                align="center",
                justify="between",
                width="100%",
            ),
        )
    )


def _save_controls() -> rx.Component:
    return component(
        rx.vstack(
            rx.text(
                "Save under",
                color="var(--gray-11)",
                font_size="0.72rem",
                font_weight="650",
            ),
            rx.flex(
                rx.text(
                    "scenarios/",
                    color="var(--gray-10)",
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.76rem",
                    padding="0.55rem 0 0.55rem 0.7rem",
                ),
                rx.input(
                    value=DashboardState.draft_directory,
                    on_change=DashboardState.change_draft_directory,
                    aria_label="Scenario folder beneath scenarios",
                    disabled=DashboardState.draft_saving,
                    max_length=MAX_SCENARIO_DIRECTORY_HINT_LENGTH,
                    placeholder="generated/api or Payments Team / Regression Cases",
                    flex="1",
                    min_width="12rem",
                    **FOCUS_STYLE,
                ),
                align="center",
                background=SUBTLE_BACKGROUND,
                border_radius="9px",
                gap="2",
                width="100%",
                wrap="wrap",
            ),
            rx.flex(
                rx.text(
                    "Keep Plantain's suggestion or enter a friendly nested folder. "
                    "The location always remains inside scenarios.",
                    color="var(--gray-9)",
                    font_size="0.7rem",
                ),
                rx.spacer(),
                rx.button(
                    rx.icon("save", size=15),
                    rx.cond(DashboardState.draft_saving, "Saving…", "Save test"),
                    aria_label="Save validated scenario locally",
                    disabled=DashboardState.draft_saving,
                    on_click=DashboardState.save_draft,
                    size="2",
                    **FOCUS_STYLE,
                ),
                align="center",
                gap="3",
                width="100%",
                wrap="wrap",
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


def _saved_run_button() -> rx.Component:
    active_ids = cast("Any", ScenarioCatalogState.test_active_run_ids)
    active = active_ids.contains(DashboardState.saved_scenario_id)
    return component(
        rx.button(
            rx.cond(
                active,
                rx.spinner(size="1"),
                rx.icon("play", size=15),
            ),
            rx.cond(active, "In progress", "Run now"),
            aria_label=DashboardState.saved_scenario_name + " — run saved scenario",
            color_scheme="blue",
            disabled=active,
            on_click=cast("Any", ScenarioCatalogState.run_test)(
                DashboardState.saved_scenario_id,
                DashboardState.saved_scenario_name,
            ),
            size="2",
            **FOCUS_STYLE,
        )
    )


def draft_review_panel() -> rx.Component:
    """Render one page of a complete backend-owned YAML draft."""

    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.hstack(
                        rx.box(
                            rx.icon("file-check-2", size=18, color=BRAND_BLUE),
                            background="var(--blue-3)",
                            border_radius="9px",
                            padding="0.55rem",
                        ),
                        rx.vstack(
                            rx.text(
                                "Validated scenario draft",
                                font_size="0.72rem",
                                font_weight="650",
                                color="var(--gray-9)",
                            ),
                            rx.heading(DashboardState.draft_scenario, size="4"),
                            align="start",
                            spacing="1",
                        ),
                        align="center",
                        spacing="3",
                    ),
                    _status(),
                    align="start",
                    spacing="3",
                ),
                rx.spacer(),
                rx.button(
                    "Dismiss",
                    aria_label="Dismiss unsaved scenario draft",
                    disabled=DashboardState.draft_saving,
                    on_click=DashboardState.dismiss_draft,
                    size="2",
                    variant="ghost",
                    **FOCUS_STYLE,
                ),
                align="start",
                gap="3",
                width="100%",
                wrap="wrap",
            ),
            rx.grid(
                _detail("Steps", DashboardState.draft_step_count),
                _detail("Activities", DashboardState.draft_activities),
                _detail("State", "Ready to save"),
                columns={"initial": "1", "sm": "3"},
                gap="3",
                width="100%",
            ),
            rx.box(
                rx.el.pre(
                    DashboardState.draft_content,
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.76rem",
                    line_height="1.55",
                    margin="0",
                    min_width="max-content",
                ),
                aria_label="Validated scenario YAML",
                background=SUBTLE_BACKGROUND,
                border_radius="10px",
                max_height="430px",
                overflow="auto",
                padding=COMPACT_PANEL_PADDING,
                role="region",
                width="100%",
            ),
            _pagination(),
            _save_controls(),
            rx.hstack(
                rx.icon("info", size=14, color=BRAND_YELLOW),
                rx.text(
                    "Saving creates reviewable YAML for CLI or dashboard use. "
                    "It does not run the test.",
                    color="var(--gray-10)",
                    font_size="0.74rem",
                ),
                align="center",
                spacing="2",
                width="100%",
            ),
            align="stretch",
            background="var(--gray-1)",
            border="1px solid var(--gray-6)",
            border_radius="12px",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
        )
    )


def saved_scenario_panel() -> rx.Component:
    """Confirm a local save without implying that execution occurred."""

    return component(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.box(
                        rx.icon("badge-check", size=18, color=BRAND_BLUE),
                        background="var(--blue-3)",
                        border_radius="9px",
                        padding="0.55rem",
                    ),
                    rx.vstack(
                        rx.text(
                            "Saved locally",
                            color="var(--gray-9)",
                            font_size="0.72rem",
                            font_weight="650",
                        ),
                        rx.heading(DashboardState.saved_scenario_name, size="4"),
                        align="start",
                        spacing="1",
                    ),
                    align="center",
                    spacing="3",
                ),
                rx.spacer(),
                rx.badge("Not run", color_scheme="yellow", variant="soft"),
                align="center",
                gap="3",
                width="100%",
                wrap="wrap",
            ),
            rx.box(
                DashboardState.saved_scenario_path,
                aria_label="Saved scenario path",
                background=SUBTLE_BACKGROUND,
                border_radius="9px",
                font_family=MONO_FONT_FAMILY,
                font_size="0.76rem",
                padding="0.7rem",
                width="100%",
            ),
            rx.text(
                "This is ordinary Plantain YAML and is available to both the CLI "
                "and dashboard. Execution remains a separate choice.",
                color="var(--gray-10)",
                font_size="0.76rem",
            ),
            rx.flex(
                _saved_run_button(),
                action_link(
                    "View in Tests",
                    icon="list-checks",
                    href="/tests",
                    variant="soft",
                ),
                rx.button(
                    "Dismiss",
                    aria_label="Dismiss saved scenario confirmation",
                    on_click=DashboardState.dismiss_saved_scenario,
                    size="2",
                    variant="ghost",
                    **FOCUS_STYLE,
                ),
                align="center",
                gap="2",
                justify="end",
                width="100%",
                wrap="wrap",
            ),
            align="stretch",
            background="var(--gray-1)",
            border="1px solid var(--gray-6)",
            border_radius="12px",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
        )
    )


__all__ = ["draft_review_panel", "saved_scenario_panel"]
