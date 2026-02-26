"""Shared identity resolution utilities for bot form handlers.

Form submissions can originate from multiple bot providers (Teams, Slack).
This module centralizes provider detection and external identity lookup so
handlers enforce the same ownership checks regardless of channel.
"""

from __future__ import annotations

from typing import Any

from src.domain.models.master_tables.external_identity_models import ExternalIdentityProvider
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.logger import get_logger
from src.service.bots.identity_service import ExternalIdentityService

logger = get_logger(__name__)


def get_provider_identity(activity: dict[str, Any]) -> tuple[ExternalIdentityProvider, str, str]:
    """Extract provider + external identity tuple from an incoming activity."""
    provider_raw = (activity.get("provider") or "").lower()
    from_user = activity.get("from", {})
    external_user_id = from_user.get("id", "")
    channel_data = activity.get("channelData", {})

    if provider_raw == "slack":
        external_tenant_id = channel_data.get("team", {}).get("id", "")
        return ExternalIdentityProvider.SLACK, external_user_id, external_tenant_id

    # Default to Teams for backwards compatibility with existing activity payloads.
    external_tenant_id = channel_data.get("tenant", {}).get("id", "")
    return ExternalIdentityProvider.TEAMS, external_user_id, external_tenant_id


async def resolve_user_from_activity(activity: dict[str, Any]):
    """Resolve a CopilotGTM user from provider-specific identity in activity."""
    provider, external_user_id, external_tenant_id = get_provider_identity(activity)
    identity_service = ExternalIdentityService()

    try:
        async for master_session in acquire_master_async_session():
            return await identity_service.get_user_for_external_identity(
                master_session,
                provider,
                external_user_id,
                external_tenant_id,
            )
    except Exception:
        logger.exception(
            "Failed to resolve external identity provider=%s user=%s tenant=%s",
            provider.value,
            external_user_id,
            external_tenant_id,
        )

    return None, None
