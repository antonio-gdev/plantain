"""Evidence-backed critical-decision plan review surfaces."""

from __future__ import annotations

from typing import Any, cast

import reflex as rx

from plantain.dashboard.shell import component
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


def _eyebrow(value: str) -> rx.Component:
    return component(
        rx.text(
            value,
            color="var(--gray-9)",
            font_size="0.68rem",
            font_weight="700",
            letter_spacing="0.05em",
            text_transform="uppercase",
        )
    )


def _metric(label: str, value: Any, detail: str) -> rx.Component:
    return component(
        rx.vstack(
            _eyebrow(label),
            rx.text(
                value,
                color="var(--gray-12)",
                font_size="1.3rem",
                font_weight="750",
            ),
            rx.text(
                detail,
                color="var(--gray-9)",
                font_size="0.7rem",
            ),
            align="start",
            background=SUBTLE_BACKGROUND,
            border_radius="10px",
            padding=COMPACT_PANEL_PADDING,
            spacing="1",
            width="100%",
        )
    )


def _tag(value: Any) -> rx.Component:
    return component(
        rx.badge(
            value,
            color_scheme="gray",
            size="1",
            variant="soft",
        )
    )


def _evidence_tag(value: Any) -> rx.Component:
    return component(
        rx.badge(
            value,
            color_scheme="blue",
            font_family=MONO_FONT_FAMILY,
            size="1",
            variant="outline",
        )
    )


def _priority_badge(priority: Any) -> rx.Component:
    return component(
        rx.cond(
            priority == "critical",
            rx.badge("Critical", color_scheme="red", variant="soft"),
            rx.cond(
                priority == "high",
                rx.badge("High", color_scheme="yellow", variant="soft"),
                rx.badge(priority, color_scheme="gray", variant="soft"),
            ),
        )
    )


def _source_card(source: Any) -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.box(
                        rx.icon("file-search", size=16, color=BRAND_BLUE),
                        background="var(--blue-3)",
                        border_radius="8px",
                        padding="0.45rem",
                    ),
                    rx.vstack(
                        rx.text(
                            source["label"],
                            font_size="0.8rem",
                            font_weight="700",
                        ),
                        rx.text(
                            source["kind_label"],
                            color="var(--gray-9)",
                            font_size="0.68rem",
                        ),
                        align="start",
                        spacing="0",
                    ),
                    align="center",
                    spacing="2",
                ),
                rx.spacer(),
                rx.cond(
                    source["truncated"],
                    rx.badge("Bounded excerpt", color_scheme="yellow", variant="soft"),
                    rx.badge("Complete excerpt", color_scheme="blue", variant="soft"),
                ),
                align="start",
                gap="2",
                width="100%",
                wrap="wrap",
            ),
            rx.text(
                source["reference"],
                color="var(--gray-10)",
                font_family=MONO_FONT_FAMILY,
                font_size="0.68rem",
                overflow_wrap="anywhere",
            ),
            _evidence_tag(source["evidence_id"]),
            align="stretch",
            background=SUBTLE_BACKGROUND,
            border_radius="10px",
            padding=COMPACT_PANEL_PADDING,
            spacing="2",
            width="100%",
        )
    )


def _dimension_card(dimension: Any) -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.icon("route", size=15, color=BRAND_BLUE),
                rx.text(
                    dimension["label"],
                    font_size="0.76rem",
                    font_weight="700",
                ),
                align="center",
                spacing="2",
            ),
            rx.text(
                dimension["summary"],
                color="var(--gray-10)",
                font_size="0.72rem",
                line_height="1.45",
            ),
            rx.flex(
                rx.foreach(dimension["evidence_ids"], _evidence_tag),
                gap="1",
                wrap="wrap",
            ),
            align="start",
            background=SUBTLE_BACKGROUND,
            border_radius="10px",
            padding=COMPACT_PANEL_PADDING,
            spacing="2",
            width="100%",
        )
    )


def _test_card(test: Any) -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.hstack(
                        _priority_badge(test["priority"]),
                        rx.text(
                            test["case_id"],
                            color="var(--gray-9)",
                            font_family=MONO_FONT_FAMILY,
                            font_size="0.68rem",
                        ),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.text(
                        test["title"],
                        font_size="0.88rem",
                        font_weight="750",
                    ),
                    align="start",
                    spacing="2",
                ),
                rx.spacer(),
                rx.button(
                    "Review case",
                    rx.icon("arrow-right", size=14),
                    aria_label=test["title"] + " — review complete planned test case",
                    disabled=DashboardState.plan_saving,
                    on_click=cast("Any", DashboardState.select_plan_case)(test["case_id"]),
                    size="1",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                align="start",
                gap="3",
                width="100%",
                wrap="wrap",
            ),
            rx.text(
                test["objective"],
                color="var(--gray-10)",
                font_size="0.74rem",
                line_height="1.5",
            ),
            rx.flex(
                rx.foreach(test["dimensions"], _tag),
                rx.cond(
                    test["summary_limited"],
                    rx.badge(
                        "Summary shortened",
                        color_scheme="yellow",
                        size="1",
                        variant="outline",
                    ),
                ),
                gap="1",
                wrap="wrap",
            ),
            align="stretch",
            background=SUBTLE_BACKGROUND,
            border_radius="10px",
            padding=COMPACT_PANEL_PADDING,
            spacing="2",
            width="100%",
        )
    )


def _detail_item(value: Any) -> rx.Component:
    return component(
        rx.hstack(
            rx.icon("arrow-right", size=13, color=BRAND_BLUE),
            rx.text(
                value,
                color="var(--gray-11)",
                font_size="0.73rem",
                line_height="1.5",
            ),
            align="start",
            spacing="2",
            width="100%",
        )
    )


def _detail_group(label: str, values: Any) -> rx.Component:
    return component(
        rx.vstack(
            _eyebrow(label),
            rx.vstack(
                rx.foreach(values, _detail_item),
                align="stretch",
                spacing="2",
                width="100%",
            ),
            align="stretch",
            background=SUBTLE_BACKGROUND,
            border_radius="10px",
            padding=COMPACT_PANEL_PADDING,
            spacing="2",
            width="100%",
        )
    )


def _case_detail() -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    _eyebrow("Selected test case"),
                    rx.heading(DashboardState.plan_case_title, size="4"),
                    rx.text(
                        DashboardState.plan_case_objective,
                        color="var(--gray-10)",
                        font_size="0.76rem",
                        line_height="1.5",
                    ),
                    align="start",
                    spacing="2",
                ),
                rx.spacer(),
                rx.button(
                    rx.icon("x", size=15),
                    aria_label="Close planned test case detail",
                    on_click=DashboardState.close_plan_case,
                    size="1",
                    variant="ghost",
                    **FOCUS_STYLE,
                ),
                align="start",
                gap="3",
                width="100%",
            ),
            rx.flex(
                _priority_badge(DashboardState.plan_case_priority),
                rx.foreach(DashboardState.plan_case_dimensions, _tag),
                gap="1",
                wrap="wrap",
            ),
            rx.grid(
                _detail_group(
                    "Preconditions",
                    DashboardState.plan_case_preconditions,
                ),
                _detail_group(
                    "Actions",
                    DashboardState.plan_case_actions,
                ),
                _detail_group(
                    "Expected results",
                    DashboardState.plan_case_expected_results,
                ),
                columns={"initial": "1", "lg": "3"},
                gap="3",
                width="100%",
            ),
            rx.vstack(
                _eyebrow("Evidence citations"),
                rx.flex(
                    rx.foreach(
                        DashboardState.plan_case_evidence_ids,
                        _evidence_tag,
                    ),
                    gap="1",
                    wrap="wrap",
                ),
                align="start",
                spacing="2",
            ),
            align="stretch",
            aria_label="Selected planned test case",
            background="var(--blue-2)",
            border="1px solid var(--blue-6)",
            border_radius="11px",
            padding="0.9rem",
            role="region",
            spacing="3",
            width="100%",
        )
    )


def _note_row(value: Any) -> rx.Component:
    return component(
        rx.hstack(
            rx.icon("circle-small", size=13, color="var(--gray-8)"),
            rx.text(
                value,
                color="var(--gray-10)",
                font_size="0.72rem",
                line_height="1.45",
            ),
            align="start",
            spacing="2",
        )
    )


def _note_panel(
    title: str,
    empty_message: str,
    values: Any,
    count: Any,
    *,
    warning: bool,
) -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.icon(
                    "triangle-alert" if warning else "circle-help",
                    size=15,
                    color=BRAND_YELLOW if warning else BRAND_BLUE,
                ),
                rx.text(title, font_size="0.78rem", font_weight="700"),
                rx.badge(count, color_scheme="gray", size="1", variant="soft"),
                align="center",
                spacing="2",
            ),
            rx.cond(
                count > 0,
                rx.vstack(
                    rx.foreach(values, _note_row),
                    align="stretch",
                    spacing="2",
                ),
                rx.text(
                    empty_message,
                    color="var(--gray-9)",
                    font_size="0.72rem",
                ),
            ),
            align="stretch",
            background=SUBTLE_BACKGROUND,
            border_radius="10px",
            padding=COMPACT_PANEL_PADDING,
            spacing="2",
            width="100%",
        )
    )


def _pagination() -> rx.Component:
    return component(
        rx.cond(
            DashboardState.plan_page_count > 1,
            rx.flex(
                rx.button(
                    rx.icon("chevron-left", size=14),
                    "Previous",
                    aria_label="Previous coverage plan page",
                    disabled=(~DashboardState.plan_has_previous | DashboardState.plan_saving),
                    on_click=DashboardState.previous_plan_page,
                    size="2",
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                rx.text(
                    "Page ",
                    DashboardState.plan_page,
                    " of ",
                    DashboardState.plan_page_count,
                    color="var(--gray-10)",
                    font_size="0.72rem",
                    role="status",
                ),
                rx.button(
                    "Next",
                    rx.icon("chevron-right", size=14),
                    aria_label="Next coverage plan page",
                    disabled=(~DashboardState.plan_has_next | DashboardState.plan_saving),
                    on_click=DashboardState.next_plan_page,
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


def _header() -> rx.Component:
    return component(
        rx.flex(
            rx.hstack(
                rx.box(
                    rx.icon("brain-circuit", size=18, color=BRAND_BLUE),
                    background="var(--blue-3)",
                    border_radius="9px",
                    padding="0.55rem",
                ),
                rx.vstack(
                    _eyebrow("Evidence-backed coverage plan"),
                    rx.heading(DashboardState.plan_feature, size="4"),
                    align="start",
                    spacing="1",
                ),
                align="center",
                spacing="3",
            ),
            rx.spacer(),
            rx.flex(
                rx.badge(
                    rx.icon("shield-check", size=13),
                    "Validated locally",
                    color_scheme="blue",
                    variant="soft",
                ),
                rx.cond(
                    DashboardState.plan_repaired,
                    rx.badge(
                        rx.icon("sparkles", size=13),
                        "Repaired and revalidated",
                        color_scheme="yellow",
                        variant="soft",
                    ),
                ),
                rx.button(
                    "Dismiss",
                    aria_label="Dismiss unsaved coverage plan",
                    disabled=DashboardState.plan_saving,
                    on_click=DashboardState.dismiss_plan,
                    size="2",
                    variant="ghost",
                    **FOCUS_STYLE,
                ),
                align="center",
                gap="2",
                wrap="wrap",
            ),
            align="start",
            gap="3",
            width="100%",
            wrap="wrap",
        )
    )


def _coverage_section() -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    _eyebrow("Coverage map"),
                    rx.text(
                        "Why each dimension matters and which local evidence supports it.",
                        color="var(--gray-9)",
                        font_size="0.72rem",
                    ),
                    align="start",
                    spacing="1",
                ),
                rx.spacer(),
                rx.badge(
                    DashboardState.plan_dimension_count,
                    " dimensions",
                    color_scheme="blue",
                    variant="soft",
                ),
                align="center",
                width="100%",
            ),
            rx.grid(
                rx.foreach(DashboardState.plan_dimensions, _dimension_card),
                columns={"initial": "1", "md": "2", "xl": "3"},
                gap="3",
                width="100%",
            ),
            align="stretch",
            spacing="3",
            width="100%",
        )
    )


def _evidence_section() -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    _eyebrow("Evidence sources"),
                    rx.text(
                        "Provenance only. Attached and snapshot content remains backend-owned.",
                        color="var(--gray-9)",
                        font_size="0.72rem",
                    ),
                    align="start",
                    spacing="1",
                ),
                rx.spacer(),
                rx.badge(
                    DashboardState.plan_source_count,
                    " sources",
                    color_scheme="gray",
                    variant="soft",
                ),
                align="center",
                width="100%",
            ),
            rx.grid(
                rx.foreach(DashboardState.plan_sources, _source_card),
                columns={"initial": "1", "md": "2"},
                gap="3",
                width="100%",
            ),
            align="stretch",
            spacing="3",
            width="100%",
        )
    )


def _tests_section() -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    _eyebrow("Planned tests"),
                    rx.text(
                        "Prioritized cases derived from the smallest high-value decision space.",
                        color="var(--gray-9)",
                        font_size="0.72rem",
                    ),
                    align="start",
                    spacing="1",
                ),
                rx.spacer(),
                rx.badge(
                    DashboardState.plan_test_count,
                    " cases",
                    color_scheme="blue",
                    variant="soft",
                ),
                align="center",
                width="100%",
            ),
            rx.vstack(
                rx.foreach(DashboardState.plan_tests, _test_card),
                align="stretch",
                spacing="0",
                width="100%",
            ),
            _pagination(),
            rx.cond(
                DashboardState.plan_case_id != "",
                _case_detail(),
            ),
            align="stretch",
            spacing="3",
            width="100%",
        )
    )


def _save_footer() -> rx.Component:
    return component(
        rx.flex(
            rx.hstack(
                rx.icon("info", size=14, color=BRAND_YELLOW),
                rx.text(
                    "Saving creates a reviewable local coverage artifact. "
                    "It does not generate or run tests.",
                    color="var(--gray-10)",
                    font_size="0.72rem",
                ),
                align="center",
                spacing="2",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("save", size=15),
                rx.cond(DashboardState.plan_saving, "Saving…", "Save coverage plan"),
                aria_label="Save evidence-backed coverage plan locally",
                disabled=DashboardState.plan_saving,
                on_click=DashboardState.save_plan,
                size="2",
                **FOCUS_STYLE,
            ),
            align="center",
            gap="3",
            width="100%",
            wrap="wrap",
        )
    )


def decision_plan_panel() -> rx.Component:
    """Render one validated, evidence-cited critical-decision plan."""

    return component(
        rx.vstack(
            _header(),
            rx.grid(
                _metric("Planned tests", DashboardState.plan_test_count, "Prioritized cases"),
                _metric(
                    "Coverage dimensions",
                    DashboardState.plan_dimension_count,
                    "Critical design lenses",
                ),
                _metric(
                    "Evidence sources",
                    DashboardState.plan_source_count,
                    "Cited local inputs",
                ),
                columns={"initial": "1", "sm": "3"},
                gap="3",
                width="100%",
            ),
            _coverage_section(),
            rx.grid(
                _note_panel(
                    "Assumptions",
                    "No assumptions were retained.",
                    DashboardState.plan_assumptions,
                    DashboardState.plan_assumption_count,
                    warning=False,
                ),
                _note_panel(
                    "Evidence gaps",
                    "No unresolved evidence gaps were identified.",
                    DashboardState.plan_gaps,
                    DashboardState.plan_gap_count,
                    warning=True,
                ),
                columns={"initial": "1", "md": "2"},
                gap="3",
                width="100%",
            ),
            _evidence_section(),
            _tests_section(),
            _save_footer(),
            align="stretch",
            background="var(--gray-1)",
            border="1px solid var(--gray-6)",
            border_radius="12px",
            padding=PANEL_PADDING,
            spacing="5",
            width="100%",
        )
    )


def saved_decision_plan_panel() -> rx.Component:
    """Confirm a local plan save without implying generation or execution."""

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
                        _eyebrow("Coverage plan saved locally"),
                        rx.heading(DashboardState.saved_plan_feature, size="4"),
                        align="start",
                        spacing="1",
                    ),
                    align="center",
                    spacing="3",
                ),
                rx.spacer(),
                rx.badge("Not generated or run", color_scheme="yellow", variant="soft"),
                align="center",
                gap="3",
                width="100%",
                wrap="wrap",
            ),
            rx.box(
                DashboardState.saved_plan_path,
                aria_label="Saved coverage plan path",
                background=SUBTLE_BACKGROUND,
                border_radius="9px",
                font_family=MONO_FONT_FAMILY,
                font_size="0.76rem",
                padding="0.7rem",
                width="100%",
            ),
            rx.flex(
                rx.badge(
                    DashboardState.saved_plan_test_count,
                    " planned tests",
                    color_scheme="blue",
                    variant="soft",
                ),
                rx.badge(
                    DashboardState.saved_plan_source_count,
                    " evidence sources",
                    color_scheme="gray",
                    variant="soft",
                ),
                rx.spacer(),
                rx.button(
                    "Dismiss",
                    aria_label="Dismiss saved coverage plan confirmation",
                    on_click=DashboardState.dismiss_saved_plan,
                    size="2",
                    variant="ghost",
                    **FOCUS_STYLE,
                ),
                align="center",
                gap="2",
                width="100%",
                wrap="wrap",
            ),
            rx.hstack(
                rx.icon("shield-check", size=14, color=BRAND_BLUE),
                rx.text(
                    "The saved artifact contains cited provenance, not attached source "
                    "or snapshot content.",
                    color="var(--gray-10)",
                    font_size="0.72rem",
                ),
                align="center",
                spacing="2",
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


__all__ = ["decision_plan_panel", "saved_decision_plan_panel"]
