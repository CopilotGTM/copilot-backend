"""Channel-specific card builders.

Factory function to get the appropriate card builder for a channel.
Card builders convert channel-agnostic definitions into
channel-specific payloads (Teams Adaptive Cards, Slack Block Kit, etc.).
"""

from src.adapters.card_builders.base import (
    FormCardBuilder,
    FormDefinition,
    FormField,
)
from src.adapters.card_builders.slack_card_builder import SlackFormCardBuilder
from src.adapters.card_builders.teams_card_builder import TeamsFormCardBuilder


def get_card_builder(channel: str) -> FormCardBuilder:
    """Get the appropriate form card builder for a channel.

    Args:
        channel: Channel identifier ("teams", "slack", etc.)

    Returns:
        FormCardBuilder instance for the specified channel

    Raises:
        ValueError: If no card builder exists for the channel
    """
    channel = channel.lower()

    if channel == "teams":
        return TeamsFormCardBuilder()
    if channel == "slack":
        return SlackFormCardBuilder()

    raise ValueError(f"No card builder for channel: {channel}. Supported: teams, slack")


__all__ = [
    "FormCardBuilder",
    "FormDefinition",
    "FormField",
    "TeamsFormCardBuilder",
    "SlackFormCardBuilder",
    "get_card_builder",
]
