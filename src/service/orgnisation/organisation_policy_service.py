"""Organisation-level app governance policy checks."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import Organisation
from src.service.orgnisation.organisation_preferences import (
    has_crm_write_scope,
    normalize_organisation_preferences,
)


class OrganisationPolicyDeniedError(Exception):
    """Raised when an organisation policy blocks an attempted action."""

    def __init__(self, message: str, *, code: str):
        self.message = message
        self.code = code
        super().__init__(message)


class OrganisationPolicyService:
    """Read and enforce organisation-level CRM and channel governance."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_policy_snapshot(self, organisation_id: UUID) -> dict[str, Any]:
        organisation = await self._session.get(Organisation, organisation_id)
        return normalize_organisation_preferences(
            organisation.preferences if organisation else None
        )

    async def has_crm_write_scope(self, organisation_id: UUID, scope: str) -> bool:
        preferences = await self.get_policy_snapshot(organisation_id)
        return has_crm_write_scope(preferences, scope)

    async def require_crm_write_scope(self, organisation_id: UUID, scope: str) -> None:
        if not await self.has_crm_write_scope(organisation_id, scope):
            raise OrganisationPolicyDeniedError(
                f"Internal CRM write scope '{scope}' is disabled for this organisation",
                code="crm_write_scope_disabled",
            )

    async def is_channel_enabled(self, organisation_id: UUID, channel: str) -> bool:
        preferences = await self.get_policy_snapshot(organisation_id)
        enabled = preferences.get("channels", {}).get("enabled", [])
        return channel in enabled

    async def require_channel_enabled(self, organisation_id: UUID, channel: str) -> None:
        if not await self.is_channel_enabled(organisation_id, channel):
            raise OrganisationPolicyDeniedError(
                f"Messaging channel '{channel}' is disabled for this organisation",
                code="channel_disabled",
            )
