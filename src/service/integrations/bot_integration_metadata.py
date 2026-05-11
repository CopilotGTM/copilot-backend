"""Static metadata for bot providers shown under Integrations."""

from dataclasses import dataclass
from typing import Literal

BotProvider = Literal["slack"]


@dataclass(frozen=True)
class BotIntegrationMetadata:
    provider: BotProvider
    name: str
    display_name: str
    description: str
    logo_url: str
    availability_status: str = "available"


SLACK_BOT_METADATA = BotIntegrationMetadata(
    provider="slack",
    name="Slack Bot",
    display_name="CopilotGTM Slack Bot",
    description="Chat with CopilotGTM from Slack DMs, mentions, and supported workflows.",
    logo_url="/integrations/slack.png",
)


def get_bot_integration_metadata() -> tuple[BotIntegrationMetadata, ...]:
    """Return bot providers currently supported by the management API."""
    return (SLACK_BOT_METADATA,)
