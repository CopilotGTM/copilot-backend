"""Resolve provider parent integrations plus capability sync state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationSyncState,
    IntegrationType,
    KnowledgeIntegration,
)

GOOGLE_CAPABILITIES = {IntegrationType.GMAIL, IntegrationType.GOOGLE_CALENDAR}
MICROSOFT_CAPABILITIES = {
    IntegrationType.OUTLOOK_EMAIL,
    IntegrationType.OUTLOOK_CALENDAR,
    IntegrationType.TEAMS,
}
CAPABILITY_PROVIDER: dict[IntegrationType, IntegrationType] = {
    **dict.fromkeys(GOOGLE_CAPABILITIES, IntegrationType.GOOGLE),
    **dict.fromkeys(MICROSOFT_CAPABILITIES, IntegrationType.MICROSOFT),
}


@dataclass(frozen=True)
class CapabilitySyncSnapshot:
    """Detached sync context safe to carry across provider API calls."""

    organisation_id: UUID
    integration_id: UUID
    sync_state_id: UUID
    provider: IntegrationType
    capability: IntegrationType
    connection_id: str
    integration_email: str | None
    credentials: dict[str, Any]
    scopes: list[str]
    cursor_metadata: dict[str, Any]
    webhook_metadata: dict[str, Any]
    sync_metadata: dict[str, Any]
    last_synced_at: datetime | None
    access_token_expires_at: datetime | None


class CapabilitySyncStateResolver:
    """Load an active provider parent and an enabled capability child."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def resolve(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        capability: IntegrationType,
    ) -> CapabilitySyncSnapshot:
        provider = CAPABILITY_PROVIDER.get(capability)
        if provider is None:
            raise ValueError(f"Unsupported provider capability: {capability.value}")

        integration_stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == organisation_id,
            col(KnowledgeIntegration.integration_id) == integration_id,
        )
        integration = (await self.session.execute(integration_stmt)).scalar_one_or_none()
        if integration is None:
            raise ValueError("Integration not found")

        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        if integration_type != provider:
            raise ValueError(
                f"Integration {integration_id} is {integration_type.value}, not {provider.value}"
            )
        if not integration.is_active:
            raise ValueError("Integration is not active")
        if not integration.connection_id:
            raise ValueError("Integration is not connected")

        state_stmt = select(IntegrationSyncState).where(
            col(IntegrationSyncState.organisation_id) == organisation_id,
            col(IntegrationSyncState.integration_id) == integration_id,
            col(IntegrationSyncState.capability) == capability,
        )
        state = (await self.session.execute(state_stmt)).scalar_one_or_none()
        if state is None:
            raise ValueError("Capability sync state not found")
        if not state.enabled:
            raise ValueError("Capability is not enabled")

        return CapabilitySyncSnapshot(
            organisation_id=organisation_id,
            integration_id=integration.integration_id,
            sync_state_id=state.sync_state_id,
            provider=provider,
            capability=capability,
            connection_id=str(integration.connection_id),
            integration_email=integration.integration_email,
            credentials=dict(integration.credentials or {}),
            scopes=list(integration.scopes or []),
            cursor_metadata=dict(state.cursor_metadata or {}),
            webhook_metadata=dict(state.webhook_metadata or {}),
            sync_metadata=dict(state.sync_metadata or {}),
            last_synced_at=state.last_synced_at,
            access_token_expires_at=integration.access_token_expires_at,
        )


def provider_for_capability(capability: IntegrationType) -> IntegrationType:
    provider = CAPABILITY_PROVIDER.get(capability)
    if provider is None:
        raise ValueError(f"Unsupported provider capability: {capability.value}")
    return provider
