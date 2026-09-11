"""Intent-first test creation surface for the local dashboard."""

from __future__ import annotations

from typing import Any

import reflex as rx

from plantain.dashboard.api_contract_panel import api_contract_panel
from plantain.dashboard.context_panel import context_drawer
from plantain.dashboard.draft_review_panel import (
    draft_review_panel,
    saved_scenario_panel,
)
from plantain.dashboard.plan_review_panel import (
    decision_plan_panel,
    saved_decision_plan_panel,
)
from plantain.dashboard.scenario_operation_panel import scenario_operation_panel
from plantain.dashboard.shell import action_link, component, page_shell
from plantain.dashboard.state import DashboardState
from plantain.dashboard.tests_page import run_activity
from plantain.dashboard.theme import (
    BRAND_BLUE,
    BRAND_YELLOW,
    FOCUS_STYLE,
    MONO_FONT_FAMILY,
    PANEL_PADDING,
    PANEL_STYLE,
    SUBTLE_BACKGROUND,
)


def _connection_badge() -> rx.Component:
    return component(
        rx.cond(
            DashboardState.agent_ready,
            rx.badge(
                rx.icon("circle-check", size=14),
                "Agent ready",
                color_scheme="blue",
                variant="soft",
            ),
            rx.badge(
                rx.icon("circle-alert", size=14),
                "Setup required",
                color_scheme="yellow",
                variant="soft",
            ),
        )
    )


def _workspace_header() -> rx.Component:
    return component(
        rx.flex(
            rx.hstack(
                rx.box(
                    rx.icon("sparkles", size=18, color=BRAND_BLUE),
                    background="var(--blue-3)",
                    border_radius="9px",
                    padding="0.55rem",
                ),
                rx.vstack(
                    rx.heading("Agent workspace", size="5"),
                    rx.text(
                        rx.cond(
                            DashboardState.agent_provider_name != "",
                            DashboardState.agent_provider_name,
                            "No provider selected",
                        ),
                        color="var(--gray-10)",
                        font_size="0.74rem",
                    ),
                    align="start",
                    spacing="0",
                ),
                _connection_badge(),
                align="center",
                spacing="3",
                wrap="wrap",
            ),
            rx.spacer(),
            action_link(
                "Agent settings",
                icon="settings",
                href="/settings",
                variant="soft",
            ),
            align={"initial": "start", "sm": "center"},
            direction={"initial": "column", "sm": "row"},
            gap="3",
            width="100%",
        )
    )


def _composer_panel() -> rx.Component:
    return component(
        rx.form(
            rx.vstack(
                rx.hstack(
                    rx.text(
                        rx.cond(
                            DashboardState.intent_action == "clarify",
                            "Add the missing detail",
                            "What should Plantain test?",
                        ),
                        font_size="0.82rem",
                        font_weight="700",
                    ),
                    rx.spacer(),
                    context_drawer(),
                    align="center",
                    spacing="3",
                    width="100%",
                ),
                rx.text_area(
                    name="intent",
                    placeholder=rx.cond(
                        DashboardState.intent_action == "clarify",
                        "Provide the missing detail.",
                        "Tell me what you're trying to test.",
                    ),
                    aria_label="Testing intent",
                    auto_focus=True,
                    auto_height=True,
                    enter_key_submit=True,
                    max_length=8_000,
                    min_height="108px",
                    min_length=1,
                    required=True,
                    resize="vertical",
                    width="100%",
                    **FOCUS_STYLE,
                ),
                rx.hstack(
                    rx.spacer(),
                    rx.button(
                        rx.icon("send", size=16),
                        "Send",
                        type="submit",
                        color_scheme="blue",
                        disabled=DashboardState.agent_connection_state != "ready",
                        loading=DashboardState.agent_busy,
                        **FOCUS_STYLE,
                    ),
                    align="center",
                    width="100%",
                ),
                align="stretch",
                background=SUBTLE_BACKGROUND,
                border_radius="12px",
                bottom="1rem",
                box_shadow="0 10px 28px rgba(15, 23, 42, 0.12)",
                padding=PANEL_PADDING,
                position="sticky",
                spacing="3",
                width="100%",
                z_index="10",
            ),
            on_submit=DashboardState.route_intent,
            reset_on_submit=True,
            id="test-intent",
            width="100%",
        )
    )


def _required_input(item: Any) -> rx.Component:
    return component(
        rx.badge(
            item,
            color_scheme="yellow",
            size="2",
            variant="soft",
        )
    )


def _usage() -> rx.Component:
    return component(
        rx.cond(
            DashboardState.agent_usage_available,
            rx.hstack(
                rx.text(
                    "Provider usage",
                    color="var(--gray-9)",
                    font_size="0.7rem",
                    font_weight="650",
                ),
                rx.spacer(),
                rx.text(
                    DashboardState.agent_input_tokens,
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.72rem",
                ),
                rx.text("in", color="var(--gray-9)", font_size="0.7rem"),
                rx.text(
                    DashboardState.agent_output_tokens,
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.72rem",
                ),
                rx.text("out", color="var(--gray-9)", font_size="0.7rem"),
                rx.text(
                    DashboardState.agent_total_tokens,
                    font_family=MONO_FONT_FAMILY,
                    font_size="0.72rem",
                ),
                rx.text("total", color="var(--gray-9)", font_size="0.7rem"),
                align="center",
                background=SUBTLE_BACKGROUND,
                border_radius="9px",
                padding="0.65rem",
                spacing="2",
                width="100%",
                wrap="wrap",
            ),
        )
    )


def _decision() -> rx.Component:
    return component(
        rx.vstack(
            rx.hstack(
                rx.badge(
                    DashboardState.intent_capability,
                    color_scheme="blue",
                    variant="soft",
                ),
                rx.cond(
                    DashboardState.intent_action == "clarify",
                    rx.badge(
                        "Needs one detail",
                        color_scheme="yellow",
                        variant="soft",
                    ),
                ),
                align="center",
                wrap="wrap",
            ),
            rx.cond(
                DashboardState.intent_summary != "",
                rx.heading(DashboardState.intent_summary, size="4"),
            ),
            rx.cond(
                DashboardState.intent_action == "clarify",
                rx.vstack(
                    rx.callout(
                        DashboardState.intent_question,
                        icon="message-square",
                        color_scheme="yellow",
                        role="status",
                        width="100%",
                    ),
                    rx.flex(
                        rx.foreach(
                            DashboardState.intent_required_inputs,
                            _required_input,
                        ),
                        gap="2",
                        wrap="wrap",
                    ),
                    align="stretch",
                    spacing="3",
                    width="100%",
                ),
            ),
            rx.cond(
                DashboardState.api_schema_version != "",
                api_contract_panel(),
            ),
            rx.cond(
                DashboardState.scenario_operation != "",
                scenario_operation_panel(),
            ),
            rx.cond(
                DashboardState.draft_id != "",
                draft_review_panel(),
            ),
            rx.cond(
                DashboardState.plan_id != "",
                decision_plan_panel(),
            ),
            rx.cond(
                DashboardState.saved_scenario_id != "",
                saved_scenario_panel(),
            ),
            rx.cond(
                DashboardState.saved_plan_path != "",
                saved_decision_plan_panel(),
            ),
            _usage(),
            align="stretch",
            spacing="4",
            width="100%",
        )
    )


def _result_panel() -> rx.Component:
    return component(
        rx.vstack(
            rx.cond(
                DashboardState.agent_error_message != "",
                rx.callout(
                    DashboardState.agent_error_message,
                    icon="triangle-alert",
                    color_scheme="red",
                    role="alert",
                    width="100%",
                ),
            ),
            run_activity(),
            rx.cond(
                DashboardState.agent_busy,
                rx.hstack(
                    rx.spinner(size="2"),
                    rx.text(
                        "Working",
                        color=BRAND_YELLOW,
                        font_size="0.76rem",
                        font_weight="650",
                    ),
                    align="center",
                    role="status",
                ),
            ),
            rx.cond(
                DashboardState.intent_action != "",
                _decision(),
                rx.center(
                    rx.vstack(
                        rx.box(
                            rx.icon("sparkles", size=22, color=BRAND_BLUE),
                            background="var(--blue-3)",
                            border_radius="10px",
                            padding="0.7rem",
                        ),
                        rx.text(
                            "Describe a test below to begin.",
                            color="var(--gray-10)",
                            font_size="0.8rem",
                            text_align="center",
                        ),
                        align="center",
                        spacing="2",
                    ),
                    min_height="210px",
                    width="100%",
                ),
            ),
            align="stretch",
            flex="1",
            min_height="300px",
            spacing="4",
            width="100%",
        )
    )


def create_page() -> rx.Component:
    """Render intent-first test creation without exposing internal skill selection."""

    content = rx.vstack(
        rx.heading(
            "CREATE",
            size="4",
            letter_spacing="-0.025em",
        ),
        rx.vstack(
            _workspace_header(),
            _result_panel(),
            _composer_panel(),
            align="stretch",
            min_height="calc(100vh - 11rem)",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        ),
        align="stretch",
        max_width="1100px",
        spacing="4",
        width="100%",
    )
    return page_shell(component(content), active="create", title="Create")


__all__ = ["create_page"]
