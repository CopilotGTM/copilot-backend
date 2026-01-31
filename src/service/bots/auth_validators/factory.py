"""Auth validator factory for bot providers."""

from __future__ import annotations

from src.service.bots.auth_validators.base import WebhookAuthValidator
from src.service.bots.auth_validators.teams_auth import TeamsAuthValidator


def get_auth_validator(provider: str, app_id: str) -> WebhookAuthValidator:
    """Return an auth validator for the given provider."""
    provider_key = provider.lower()
    if provider_key == "teams":
        return TeamsAuthValidator(app_id)

    raise ValueError(f"Unsupported auth provider: {provider}")
