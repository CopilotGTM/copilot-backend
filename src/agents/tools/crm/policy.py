"""Shared CRM policy checks for agent tool write paths."""

from __future__ import annotations

from uuid import UUID

from src.infra.database.sessions import acquire_master_async_session
from src.service.orgnisation.organisation_policy_service import OrganisationPolicyService


async def ensure_crm_write_scope(organisation_id: UUID, scope: str) -> None:
    async for master_session in acquire_master_async_session():
        await OrganisationPolicyService(master_session).require_crm_write_scope(
            organisation_id,
            scope,
        )
        return
