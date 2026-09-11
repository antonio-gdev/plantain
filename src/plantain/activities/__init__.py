"""Built-in activity registration."""

from __future__ import annotations

from plantain.engine.registry import ActivityRegistry


def register_framework_activities(registry: ActivityRegistry) -> None:
    """Register framework-owned YAML activities without initializing integrations early."""
    from plantain.activities.api.activities import register_api_activities  # noqa: PLC0415
    from plantain.activities.database.activities import (  # noqa: PLC0415
        register_database_activities,
    )
    from plantain.activities.ui_activity import register_ui_activity  # noqa: PLC0415

    register_ui_activity(registry)
    register_api_activities(registry)
    register_database_activities(registry)


__all__ = ["register_framework_activities"]
