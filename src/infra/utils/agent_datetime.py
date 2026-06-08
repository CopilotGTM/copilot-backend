"""Datetime parsing helpers for agent-provided user-local time expressions."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any
from zoneinfo import ZoneInfo

from src.infra.utils.timezone import (
    get_context_anchor_local,
    resolve_timezone_name,
)

_WEEKDAY_WITH_TIME_PATTERN = re.compile(
    r"\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|"
    r"sat(?:urday)?|sun(?:day)?)\b.*\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
    re.IGNORECASE,
)
_TIME_PATTERN = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", re.I)
_ISO_DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _has_explicit_time(value: str) -> bool:
    return bool(_TIME_PATTERN.search(value))


def _parse_iso_datetime(value: str, timezone_name: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(UTC)


def parse_agent_datetime(
    value: str | datetime | None,
    *,
    timezone_name: str | None,
    metadata: dict[str, Any] | None = None,
    require_time: bool = False,
    require_timezone_for_natural_language: bool = False,
) -> datetime | None:
    """Parse agent tool datetime input into UTC using the user's local context.

    Agent prompts can help the model choose a useful argument, but write/query tools
    still need deterministic normalization before side effects. Natural-language
    inputs are anchored to the request's current_datetime in the user's timezone.
    """

    if value is None:
        return None

    raw_value = value.strip() if isinstance(value, str) else None

    metadata_timezone = (metadata or {}).get("user_timezone")
    resolved_timezone = resolve_timezone_name(
        timezone_name,
        metadata_timezone if isinstance(metadata_timezone, str) else None,
        default=None,
    )
    has_valid_timezone = resolved_timezone is not None
    if resolved_timezone is None:
        if require_timezone_for_natural_language:
            return _parse_iso_datetime(raw_value, "UTC") if raw_value else None
        resolved_timezone = "UTC"

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=ZoneInfo(resolved_timezone)).astimezone(UTC)
        return value.astimezone(UTC)

    if raw_value is None or not raw_value:
        return None

    if require_time and _ISO_DATE_ONLY_PATTERN.match(raw_value):
        return None

    iso_parsed = _parse_iso_datetime(raw_value, resolved_timezone)
    if iso_parsed is not None:
        if require_time and "T" not in raw_value and " " not in raw_value:
            return None
        return iso_parsed

    if require_timezone_for_natural_language and not has_valid_timezone:
        return None

    try:
        dateparser: Any = import_module("dateparser")
        anchor_local = get_context_anchor_local(
            {**(metadata or {}), "user_timezone": resolved_timezone}
        )
        prefer_dates_from = (
            "current_period" if _WEEKDAY_WITH_TIME_PATTERN.search(raw_value) else "future"
        )
        parsed = dateparser.parse(
            raw_value,
            settings={
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": prefer_dates_from,
                "TIMEZONE": resolved_timezone,
                "TO_TIMEZONE": "UTC",
                "RELATIVE_BASE": anchor_local.replace(tzinfo=None),
            },
        )
    except Exception:
        return None

    if not parsed:
        return None

    if require_time and not _has_explicit_time(raw_value):
        return None

    parsed_utc = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    if _WEEKDAY_WITH_TIME_PATTERN.search(raw_value):
        parsed_local = parsed_utc.astimezone(ZoneInfo(resolved_timezone))
        if parsed_local < anchor_local:
            parsed_utc = (parsed_local + timedelta(days=7)).astimezone(UTC)

    return parsed_utc


__all__ = ["parse_agent_datetime"]
