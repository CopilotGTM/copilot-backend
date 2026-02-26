"""Channel-specific card builders.

Factory function to get the appropriate card builder for a channel.
Card builders convert channel-agnostic definitions into
channel-specific payloads (Teams Adaptive Cards, Slack Block Kit, etc.).
"""

from src.adapters.card_builders.base import (
    FormCardBuilder,
    FormDefinition,
    FormField,
    VisualizationCardBuilder,
    VisualizationDefinition,
    VisualizationPoint,
    VisualizationSeries,
)
from src.adapters.card_builders.slack_card_builder import (
    SlackFormCardBuilder,
    SlackVisualizationCardBuilder,
)
from src.adapters.card_builders.teams_card_builder import (
    TeamsFormCardBuilder,
    TeamsVisualizationCardBuilder,
)


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


def get_visualization_card_builder(channel: str) -> VisualizationCardBuilder:
    """Get the appropriate visualization card builder for a channel."""
    channel = channel.lower()

    if channel == "teams":
        return TeamsVisualizationCardBuilder()
    if channel == "slack":
        return SlackVisualizationCardBuilder()

    raise ValueError(
        f"No visualization card builder for channel: {channel}. Supported: teams, slack"
    )


__all__ = [
    "FormCardBuilder",
    "FormDefinition",
    "FormField",
    "VisualizationCardBuilder",
    "VisualizationDefinition",
    "VisualizationPoint",
    "VisualizationSeries",
    "TeamsFormCardBuilder",
    "TeamsVisualizationCardBuilder",
    "SlackFormCardBuilder",
    "SlackVisualizationCardBuilder",
    "get_card_builder",
    "get_visualization_card_builder",
]
