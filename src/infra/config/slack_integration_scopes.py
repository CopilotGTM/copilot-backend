"""Slack OAuth user scope sets for CopilotGTM integrations."""

from __future__ import annotations

SLACK_USER_SCOPES: tuple[str, ...] = (
    "channels:read",
    "groups:read",
    "mpim:read",
    "channels:history",
    "groups:history",
    "mpim:history",
    "users:read",
    "users:read.email",
    "team:read",
)


def get_slack_user_scopes() -> tuple[str, ...]:
    return SLACK_USER_SCOPES
