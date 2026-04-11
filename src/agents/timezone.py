"""Timezone helpers for agent runtime context."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.infra.utils.logger import get_logger
from src.infra.utils.timezone import (
    DEFAULT_TIMEZONE,
    get_context_anchor_local,
    resolve_timezone_name,
    validate_timezone_name,
)

logger = get_logger("agent_timezone")

DEFAULT_AGENT_TIMEZONE = DEFAULT_TIMEZONE


def resolve_agent_timezone_identifier(metadata: Mapping[str, Any] | None) -> str:
    """Return a valid IANA timezone for Agno datetime grounding.

    Relative time expressions like "today" and "tomorrow" should follow the user's
    local day when the channel provides a timezone. If not, fall back to UTC so
    worker region does not leak into agent behavior.
    """

    raw_timezone = metadata.get("user_timezone") if metadata else None
    timezone_name = validate_timezone_name(raw_timezone if isinstance(raw_timezone, str) else None)
    if timezone_name is None and raw_timezone:
        logger.warning(
            "Invalid agent user_timezone '%s'; falling back to %s",
            raw_timezone,
            DEFAULT_AGENT_TIMEZONE,
        )
    return (
        resolve_timezone_name(raw_timezone if isinstance(raw_timezone, str) else None)
        or DEFAULT_AGENT_TIMEZONE
    )


def get_agno_datetime_settings(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the standard Agno datetime grounding settings for this repo."""

    metadata_dict = dict(metadata or {})
    timezone_name = resolve_agent_timezone_identifier(metadata_dict)

    if metadata_dict.get("current_datetime"):
        anchored_local = get_context_anchor_local(metadata_dict)
        return {
            "add_datetime_to_context": False,
            "additional_context": (
                "The current time is "
                f"{anchored_local.isoformat()} ({timezone_name}). "
                "Use this anchored time for relative date expressions like "
                "'today', 'tomorrow', and 'next week'."
            ),
        }

    return {"add_datetime_to_context": True, "timezone_identifier": timezone_name}


__all__ = [
    "DEFAULT_AGENT_TIMEZONE",
    "get_agno_datetime_settings",
    "resolve_agent_timezone_identifier",
]
