"""Secure first-run agent setup for the local dashboard."""

from __future__ import annotations

from typing import Any

import reflex as rx

from plantain.dashboard.shell import component
from plantain.dashboard.state import DashboardState
from plantain.dashboard.theme import FOCUS_STYLE, PANEL_PADDING, PANEL_STYLE

_PROVIDER_CHOICES = (
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("deepseek", "DeepSeek"),
    ("gemini", "Gemini"),
    ("ollama", "Ollama"),
    ("lm_studio", "LM Studio"),
    ("custom", "Custom compatible"),
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


def _provider_select() -> rx.Component:
    return component(
        rx.select.root(
            rx.select.trigger(
                aria_label="Agent provider",
                variant="surface",
                width="100%",
                **FOCUS_STYLE,
            ),
            rx.select.content(
                rx.select.group(
                    *(rx.select.item(label, value=value) for value, label in _PROVIDER_CHOICES)
                ),
                position="popper",
            ),
            name="provider",
            value=DashboardState.agent_setup_provider,
            on_change=DashboardState.select_agent_provider,
            required=True,
            width="100%",
        )
    )


def _endpoint_field() -> rx.Component:
    return component(
        rx.cond(
            DashboardState.agent_setup_provider == "custom",
            _field(
                "Compatible endpoint",
                "HTTP or HTTPS origin without credentials, query, or fragment.",
                rx.input(
                    name="base_url",
                    type="url",
                    default_value=DashboardState.agent_base_url,
                    placeholder="https://agent.example.com/v1",
                    aria_label="Compatible agent endpoint",
                    max_length=2_048,
                    required=True,
                    width="100%",
                    **FOCUS_STYLE,
                ),
            ),
            rx.fragment(),
        )
    )


def _credential_field() -> rx.Component:
    local_provider = (DashboardState.agent_setup_provider == "ollama") | (
        DashboardState.agent_setup_provider == "lm_studio"
    )
    return component(
        rx.cond(
            local_provider,
            rx.callout(
                "This local provider does not require an API key.",
                icon="key-round",
                color_scheme="blue",
                role="status",
                width="100%",
            ),
            _field(
                "API key",
                rx.cond(
                    DashboardState.agent_session_configured,
                    "Leave blank to keep the current session key or use the environment.",
                    "Accepted by the backend for this process only; never saved to disk.",
                ),
                rx.input(
                    name="credential",
                    type="password",
                    placeholder="Paste key for this local session",
                    aria_label="Agent provider API key",
                    auto_complete=False,
                    max_length=16_384,
                    spell_check=False,
                    width="100%",
                    **FOCUS_STYLE,
                ),
            ),
        )
    )


def _identity_fields() -> rx.Component:
    return component(
        rx.grid(
            _field(
                "Provider",
                "Hosted, loopback-local, and compatible providers.",
                _provider_select(),
            ),
            _field(
                "Model",
                "Exact identifier available from the provider.",
                rx.input(
                    name="model",
                    default_value=DashboardState.agent_model,
                    placeholder="Model identifier",
                    aria_label="Agent model identifier",
                    max_length=160,
                    required=True,
                    width="100%",
                    **FOCUS_STYLE,
                ),
            ),
            columns={"initial": "1", "md": "2"},
            gap="4",
            width="100%",
        )
    )


def _setup_form() -> rx.Component:
    return component(
        rx.form.root(
            rx.vstack(
                _identity_fields(),
                _endpoint_field(),
                _credential_field(),
                rx.callout(
                    "Hosted requests receive only the bounded, redacted context "
                    "disclosed for that operation.",
                    icon="shield-check",
                    color_scheme="blue",
                    role="note",
                    width="100%",
                ),
                rx.button(
                    rx.icon("save", size=15),
                    "Use this profile",
                    type="submit",
                    color_scheme="blue",
                    loading=DashboardState.agent_busy,
                    align_self="start",
                    **FOCUS_STYLE,
                ),
                align="stretch",
                spacing="4",
                width="100%",
            ),
            on_submit=DashboardState.configure_agent_profile,
            reset_on_submit=True,
            width="100%",
        )
    )


def agent_settings_panel() -> rx.Component:
    """Render secure provider onboarding without retaining a key in browser state."""

    return component(
        rx.vstack(
            rx.flex(
                rx.vstack(
                    rx.heading("Agent profile", size="5"),
                    rx.text(
                        "Choose a provider and model for this dashboard session.",
                        color="var(--gray-10)",
                        font_size="0.8rem",
                    ),
                    align="start",
                    spacing="1",
                ),
                rx.spacer(),
                rx.badge(
                    rx.cond(DashboardState.agent_ready, "Ready", "Setup required"),
                    color_scheme="blue",
                    variant="soft",
                ),
                align="start",
                direction={"initial": "column", "sm": "row"},
                gap="3",
                width="100%",
            ),
            _setup_form(),
            align="stretch",
            padding=PANEL_PADDING,
            spacing="4",
            width="100%",
            **PANEL_STYLE,
        )
    )


__all__ = ["agent_settings_panel"]
