"""Auth validator factory for bot providers."""

from __future__ import annotations

from src.service.bots.auth_validators.base import WebhookAuthValidator
from src.service.bots.auth_validators.google_chat_auth import GoogleChatAuthValidator
from src.service.bots.auth_validators.slack_auth import SlackAuthValidator
from src.service.bots.auth_validators.teams_auth import TeamsAuthValidator
from src.service.bots.auth_validators.whatsapp_auth import WhatsAppAuthValidator


def get_auth_validator(provider: str, credential: str) -> WebhookAuthValidator:
    """Return an auth validator for the given provider.

    Args:
        provider: Bot platform identifier ("teams" or "slack")
        credential: For Teams: app_id. For Slack: signing_secret.
    """
    provider_key = provider.lower()
    if provider_key == "teams":
        return TeamsAuthValidator(credential)
    elif provider_key == "slack":
        return SlackAuthValidator(credential)
    elif provider_key == "whatsapp":
        return WhatsAppAuthValidator(credential)
    elif provider_key == "google_chat":
        return GoogleChatAuthValidator(credential)

    raise ValueError(f"Unsupported auth provider: {provider}")
