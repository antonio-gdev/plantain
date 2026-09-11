"""Plantain dashboard visual tokens."""

from __future__ import annotations

from typing import Any

BRAND_BLUE = "#2563EB"
BRAND_BLUE_DARK = "#1D4ED8"
BRAND_BLACK = "#111318"
BRAND_YELLOW = "#EAB308"
BRAND_RED = "#DC2626"

APP_BACKGROUND = "light-dark(#FAF9F7, var(--gray-1))"
BORDER_COLOR = "light-dark(#D8D2CA, var(--gray-5))"
COMPACT_PANEL_PADDING = "clamp(0.9rem, 1.5vw, 1.1rem)"
MUTED_TEXT_COLOR = "light-dark(#5F5A53, var(--gray-10))"
PAGE_GUTTER = "clamp(1rem, 3vw, 2rem)"
PAGE_VERTICAL_PADDING = "clamp(1rem, 2.5vw, 2rem)"
PANEL_BACKGROUND = "light-dark(#F3F0EB, var(--gray-2))"
PANEL_PADDING = "clamp(1rem, 2vw, 1.25rem)"
SUBTLE_BACKGROUND = "light-dark(#ECE8E2, var(--gray-3))"
TEXT_COLOR = "light-dark(#20242B, var(--gray-12))"
TOPBAR_BACKGROUND = "light-dark(#FAF9F7, var(--gray-2))"

FONT_FAMILY = (
    'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
)
MONO_FONT_FAMILY = '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace'

APP_STYLE: dict[str, Any] = {
    "font_family": FONT_FAMILY,
    "background": APP_BACKGROUND,
    "color": TEXT_COLOR,
    "min_height": "100vh",
    "::selection": {
        "background": BRAND_BLUE,
        "color": "#FFFFFF",
    },
}

PANEL_STYLE: dict[str, Any] = {
    "background": PANEL_BACKGROUND,
    "border": f"1px solid {BORDER_COLOR}",
    "border_radius": "14px",
    "box_shadow": "0 1px 3px rgba(32, 36, 43, 0.08)",
}

FOCUS_STYLE: dict[str, Any] = {
    "_focus_visible": {
        "outline": f"3px solid {BRAND_BLUE}",
        "outline_offset": "2px",
    }
}

__all__ = [
    "APP_BACKGROUND",
    "APP_STYLE",
    "BORDER_COLOR",
    "BRAND_BLACK",
    "BRAND_BLUE",
    "BRAND_BLUE_DARK",
    "BRAND_RED",
    "BRAND_YELLOW",
    "COMPACT_PANEL_PADDING",
    "FOCUS_STYLE",
    "FONT_FAMILY",
    "MONO_FONT_FAMILY",
    "MUTED_TEXT_COLOR",
    "PAGE_GUTTER",
    "PAGE_VERTICAL_PADDING",
    "PANEL_BACKGROUND",
    "PANEL_PADDING",
    "PANEL_STYLE",
    "SUBTLE_BACKGROUND",
    "TEXT_COLOR",
    "TOPBAR_BACKGROUND",
]
