"""Local-first Results settings with optional output destinations."""

from __future__ import annotations

from typing import Any

import reflex as rx

from plantain.dashboard.shell import component
from plantain.dashboard.state import DashboardState
from plantain.dashboard.theme import (
    BRAND_BLUE,
    FOCUS_STYLE,
    PANEL_PADDING,
    PANEL_STYLE,
    SUBTLE_BACKGROUND,
)


def _field(
    label: str,
    description: Any,
    control: rx.Component,
) -> rx.Component:
    return component(
        rx.vstack(
            rx.text(label, font_size="0.78rem", font_weight="700"),
            control,
            rx.text(
                description,
                color="var(--gray-9)",
                font_size="0.7rem",
                line_height="1.4",
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


def _setting_row(
    *,
    icon: str,
    title: str,
    description: str,
    control: rx.Component,
) -> rx.Component:
    return component(
        rx.flex(
            rx.center(
                rx.icon(icon, size=17),
                background="var(--blue-3)",
                border_radius="10px",
                color=BRAND_BLUE,
                flex_shrink="0",
                height="34px",
                width="34px",
            ),
            rx.vstack(
                rx.text(title, font_size="0.82rem", font_weight="700"),
                rx.text(
                    description,
                    color="var(--gray-10)",
                    font_size="0.72rem",
                    line_height="1.45",
                ),
                align="start",
                flex="1",
                min_width="220px",
                spacing="1",
            ),
            control,
            align="center",
            background=SUBTLE_BACKGROUND,
            border_radius="10px",
            gap="3",
            justify="between",
            padding="0.85rem",
            width="100%",
            wrap="wrap",
        )
    )


def _native_results() -> rx.Component:
    return _setting_row(
        icon="file-check-2",
        title="Plantain native result",
        description="Immutable local source for history, evidence, and projections.",
        control=component(
            rx.badge(
                "Always on",
                color_scheme="blue",
                variant="soft",
            )
        ),
    )


def _allure_results() -> rx.Component:
    return _setting_row(
        icon="chart-bar",
        title="Allure result files",
        description="Emit optional local files under output/allure.",
        control=component(
            rx.switch(
                checked=DashboardState.results_allure_enabled,
                on_change=DashboardState.set_allure_results,
                aria_label="Emit local Allure result files",
                size="2",
                **FOCUS_STYLE,
            )
        ),
    )


def _local_results_section() -> rx.Component:
    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.text("Local results", font_size="0.9rem", font_weight="750"),
                    align="start",
                    spacing="1",
                ),
                rx.spacer(),
                rx.badge("Default", color_scheme="blue", variant="soft"),
                align="start",
                direction={"initial": "column", "sm": "row"},
                gap="3",
                width="100%",
            ),
            _native_results(),
            _allure_results(),
            align="stretch",
            spacing="3",
            width="100%",
        )
    )


def _zephyr_header() -> rx.Component:
    return _setting_row(
        icon="send",
        title="Zephyr Scale Server / Data Center",
        description="Publish final status to an existing Zephyr test case.",
        control=component(
            rx.hstack(
                rx.badge("Optional", color_scheme="gray", variant="soft"),
                rx.switch(
                    checked=DashboardState.results_zephyr_enabled,
                    on_change=DashboardState.set_zephyr_results,
                    aria_label="Publish results to Zephyr Scale",
                    size="2",
                    **FOCUS_STYLE,
                ),
                align="center",
                spacing="2",
            )
        ),
    )


def _attachment_controls() -> rx.Component:
    return component(
        rx.vstack(
            _setting_row(
                icon="paperclip",
                title="Attach sanitized native result",
                description="Attach only the bounded, sanitized native result.",
                control=component(
                    rx.switch(
                        checked=DashboardState.results_zephyr_attach_report,
                        on_change=DashboardState.set_zephyr_attachment,
                        aria_label="Attach sanitized native result to Zephyr",
                        size="2",
                        **FOCUS_STYLE,
                    )
                ),
            ),
            rx.cond(
                DashboardState.results_zephyr_attach_report,
                rx.callout(
                    rx.flex(
                        rx.vstack(
                            rx.text(
                                "Approve attachment data sharing",
                                font_weight="700",
                            ),
                            rx.text(
                                "Required once for this process before Plantain may "
                                "send the sanitized result attachment.",
                                font_size="0.72rem",
                            ),
                            align="start",
                            spacing="1",
                        ),
                        rx.spacer(),
                        rx.switch(
                            checked=DashboardState.results_attachment_approved,
                            on_change=DashboardState.set_results_attachment_approval,
                            aria_label="Approve sanitized result attachment sharing",
                            size="2",
                            **FOCUS_STYLE,
                        ),
                        align={"initial": "start", "sm": "center"},
                        direction={"initial": "column", "sm": "row"},
                        gap="3",
                        width="100%",
                    ),
                    icon="shield-check",
                    color_scheme="yellow",
                    role="note",
                    width="100%",
                ),
                rx.fragment(),
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


def _zephyr_fields() -> rx.Component:
    return component(
        rx.vstack(
            rx.grid(
                _field(
                    "Jira base URL",
                    "Use the absolute Server / Data Center URL; Zephyr Cloud is unsupported.",
                    rx.input(
                        name="zephyr_base_url",
                        type="url",
                        default_value=DashboardState.results_zephyr_base_url,
                        placeholder="https://jira.example.com",
                        aria_label="Jira Server or Data Center base URL",
                        max_length=2_048,
                        required=True,
                        width="100%",
                        **FOCUS_STYLE,
                    ),
                ),
                _field(
                    "Personal access token",
                    rx.cond(
                        DashboardState.results_session_configured,
                        "Leave blank to retain the current session token or use the environment.",
                        "Accepted by the backend for this process only; never saved to disk.",
                    ),
                    rx.input(
                        name="credential",
                        type="password",
                        placeholder="Paste PAT for this local session",
                        aria_label="Zephyr personal access token",
                        auto_complete=False,
                        max_length=16_384,
                        spell_check=False,
                        width="100%",
                        **FOCUS_STYLE,
                    ),
                ),
                columns={"initial": "1", "md": "2"},
                gap="4",
                width="100%",
            ),
            rx.hstack(
                rx.text(
                    "Credential source",
                    color="var(--gray-9)",
                    font_size="0.72rem",
                ),
                rx.badge(
                    DashboardState.results_credential_source,
                    color_scheme="blue",
                    variant="soft",
                ),
                align="center",
                spacing="2",
            ),
            _attachment_controls(),
            rx.callout(
                "Publication requires a real testCaseKey; Plantain never creates Jira issues.",
                icon="info",
                color_scheme="blue",
                role="note",
                width="100%",
            ),
            align="stretch",
            spacing="4",
            width="100%",
        )
    )


def _messages() -> rx.Component:
    return component(
        rx.vstack(
            rx.cond(
                DashboardState.results_notice != "",
                rx.callout(
                    DashboardState.results_notice,
                    icon="circle-check",
                    color_scheme="blue",
                    role="status",
                    width="100%",
                ),
            ),
            rx.cond(
                DashboardState.results_error_message != "",
                rx.callout(
                    DashboardState.results_error_message,
                    icon="triangle-alert",
                    color_scheme="red",
                    role="alert",
                    width="100%",
                ),
            ),
            align="stretch",
            spacing="2",
            width="100%",
        )
    )


def results_settings_panel() -> rx.Component:
    """Render local-first result settings without exposing credential values."""

    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.heading("Results", size="5"),
                    rx.text(
                        "Local output is authoritative; integrations are optional.",
                        color="var(--gray-10)",
                        font_size="0.8rem",
                        max_width="680px",
                    ),
                    align="start",
                    spacing="1",
                ),
                rx.spacer(),
                rx.badge("Local first", color_scheme="blue", variant="soft"),
                align="start",
                direction={"initial": "column", "sm": "row"},
                gap="3",
                width="100%",
            ),
            rx.form.root(
                rx.vstack(
                    _local_results_section(),
                    rx.vstack(
                        _zephyr_header(),
                        rx.cond(
                            DashboardState.results_zephyr_enabled,
                            _zephyr_fields(),
                            rx.fragment(),
                        ),
                        align="stretch",
                        spacing="3",
                        width="100%",
                    ),
                    rx.hstack(
                        rx.button(
                            rx.icon("save", size=15),
                            "Save results settings",
                            type="submit",
                            color_scheme="blue",
                            loading=DashboardState.results_busy,
                            **FOCUS_STYLE,
                        ),
                        rx.cond(
                            DashboardState.results_session_configured,
                            rx.button(
                                rx.icon("rotate-ccw", size=15),
                                "Reset to environment",
                                type="button",
                                on_click=DashboardState.reset_results_profile,
                                disabled=DashboardState.results_busy,
                                variant="outline",
                                **FOCUS_STYLE,
                            ),
                        ),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    _messages(),
                    align="stretch",
                    spacing="4",
                    width="100%",
                ),
                on_submit=DashboardState.configure_results_profile,
                reset_on_submit=True,
                width="100%",
            ),
            align="stretch",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        )
    )


__all__ = ["results_settings_panel"]
