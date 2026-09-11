"""Agent, results, and appearance settings."""

from __future__ import annotations

from typing import Any

import reflex as rx

from plantain.dashboard.agent_settings_panel import agent_settings_panel
from plantain.dashboard.results_settings_panel import results_settings_panel
from plantain.dashboard.shell import component, page_shell
from plantain.dashboard.state import DashboardState
from plantain.dashboard.theme import (
    FOCUS_STYLE,
    PANEL_PADDING,
    PANEL_STYLE,
    SUBTLE_BACKGROUND,
)


def _connection_badge() -> rx.Component:
    return component(
        rx.cond(
            DashboardState.agent_ready,
            rx.badge("Configured", color_scheme="blue", variant="soft"),
            rx.badge("Setup required", color_scheme="yellow", variant="soft"),
        )
    )


def _value_row(label: str, value: Any) -> rx.Component:
    return component(
        rx.hstack(
            rx.text(
                label,
                color="var(--gray-9)",
                font_size="0.72rem",
                font_weight="650",
                min_width="76px",
            ),
            rx.text(value, font_size="0.82rem", font_weight="600"),
            align="center",
            background=SUBTLE_BACKGROUND,
            border_radius="9px",
            min_height="42px",
            padding="0.65rem 0.75rem",
            width="100%",
        )
    )


def _active_connection() -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.heading("Agent connection", size="5"),
                    rx.text(
                        "Environment-managed or session-only; credential values "
                        "never return to browser state.",
                        color="var(--gray-10)",
                        font_size="0.8rem",
                        max_width="650px",
                    ),
                    align="start",
                    spacing="1",
                ),
                rx.spacer(),
                _connection_badge(),
                align="start",
                direction={"initial": "column", "sm": "row"},
                gap="3",
                width="100%",
            ),
            _value_row(
                "Provider",
                rx.cond(
                    DashboardState.agent_provider_name != "",
                    DashboardState.agent_provider_name,
                    "Not selected",
                ),
            ),
            _value_row(
                "Model",
                rx.cond(
                    DashboardState.agent_model != "",
                    DashboardState.agent_model,
                    "Not selected",
                ),
            ),
            _value_row("Credential", DashboardState.agent_credential_source),
            rx.cond(
                DashboardState.agent_base_url != "",
                _value_row("Endpoint", DashboardState.agent_base_url),
            ),
            rx.text(
                DashboardState.agent_status_message,
                color="var(--gray-10)",
                font_size="0.8rem",
            ),
            rx.hstack(
                rx.button(
                    rx.icon("refresh-cw", size=15),
                    "Refresh status",
                    on_click=DashboardState.refresh_agent,
                    variant="soft",
                    **FOCUS_STYLE,
                ),
                rx.button(
                    rx.icon("plug", size=15),
                    "Test connection",
                    on_click=DashboardState.check_agent,
                    color_scheme="blue",
                    disabled=DashboardState.agent_connection_state != "ready",
                    loading=DashboardState.agent_busy,
                    **FOCUS_STYLE,
                ),
                rx.cond(
                    DashboardState.agent_session_configured,
                    rx.button(
                        rx.icon("rotate-ccw", size=15),
                        "Reset to environment",
                        on_click=DashboardState.reset_agent_profile,
                        disabled=DashboardState.agent_busy,
                        variant="outline",
                        **FOCUS_STYLE,
                    ),
                ),
                align="center",
                spacing="2",
                wrap="wrap",
            ),
            rx.cond(
                DashboardState.agent_profile_notice != "",
                rx.callout(
                    DashboardState.agent_profile_notice,
                    icon="info",
                    color_scheme="blue",
                    role="status",
                    width="100%",
                ),
            ),
            rx.cond(
                DashboardState.agent_check_state == "passed",
                rx.callout(
                    "Connection verified. The selected model returned a valid typed response.",
                    icon="circle-check",
                    color_scheme="blue",
                    role="status",
                    width="100%",
                ),
            ),
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
            align="stretch",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        )
    )


def _settings_header() -> rx.Component:
    return component(
        rx.hstack(
            rx.heading(
                "SETTINGS",
                size="4",
                letter_spacing="-0.025em",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("sun-moon", size=15),
                "Toggle theme",
                aria_label="Toggle light and dark theme",
                color_scheme="gray",
                on_click=rx.toggle_color_mode,
                size="2",
                variant="soft",
                **FOCUS_STYLE,
            ),
            align="center",
            width="100%",
        )
    )


def settings_page() -> rx.Component:
    """Render local settings without exposing secret values."""

    content = rx.vstack(
        _settings_header(),
        _active_connection(),
        agent_settings_panel(),
        results_settings_panel(),
        align="stretch",
        max_width="1080px",
        spacing="4",
        width="100%",
    )
    return page_shell(component(content), active="settings", title="Settings")


__all__ = ["settings_page"]
