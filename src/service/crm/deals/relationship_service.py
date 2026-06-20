"""Relationship orchestration for internal CRM deal writes."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from src.service.crm.accounts.write_service import AccountWriteService


class DealRelationshipService:
    """Coordinates account/contact/participant side effects for deal writes."""

    def __init__(self, org_session: AsyncSession, organisation_id):
        self.session = org_session
        self.organisation_id = organisation_id
        self._account_writer = AccountWriteService(self.session, self.organisation_id)
