"""Messaging providers for Teams, Slack, etc."""

from .teams import fetch_channels, fetch_messages

__all__ = ["fetch_channels", "fetch_messages"]
