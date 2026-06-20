"""Compatibility facade for CRM contact reads, writes, and deal linking."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.infra.utils.logger import get_logger
from src.service.crm.accounts.contracts import AccountWriteContext, AccountWriteServiceError
from src.service.crm.accounts.read_service import AccountReadService
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.contacts.contracts import (
    ContactDealLinkResult,
    ContactDealLinkServiceError,
    ContactReadServiceError,
    ContactWriteServiceError,
)
from src.service.crm.contacts.deal_link_service import ContactDealLinkService
from src.service.crm.contacts.read_service import ContactReadService
from src.service.crm.contacts.write_service import ContactWriteService

logger = get_logger("contacts_access_service")


class ContactsAccessServiceError(ContactReadServiceError):
    """Compatibility error for callers that still import the old access service."""


class ContactsAccessService:
    """Deprecated compatibility facade; use contact read, write, or deal-link services."""

    _extract_domain = staticmethod(ContactReadService._extract_domain)
    _split_name = staticmethod(ContactWriteService._split_name)
    _map_source = staticmethod(ContactReadService._map_source)

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Initialize the compatibility facade with an org-scoped session."""
        self.org_session = org_session
        self.organisation_id = organisation_id
        self._reader = ContactReadService(org_session, organisation_id)
        self._writer = ContactWriteService(org_session, organisation_id)
        self._deal_linker = ContactDealLinkService(org_session, organisation_id)
        self._account_reader = AccountReadService(org_session, organisation_id)
        self._account_writer = AccountWriteService(org_session, organisation_id)

    async def close(self) -> None:
        """Clean up any resources."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("ContactsAccessService close exception", exc_info=True)

    @staticmethod
    def _to_access_error(
        exc: ContactReadServiceError | ContactWriteServiceError | ContactDealLinkServiceError,
    ) -> ContactsAccessServiceError:
        return ContactsAccessServiceError(exc.message, status_code=exc.status_code, data=exc.data)

    async def resolve_source_for_contact(self, integration_id: UUID) -> str:
        """Look up the integration type for a single contact and return its source label."""
        try:
            return await self._reader.resolve_source_for_contact(integration_id)
        except ContactReadServiceError as exc:
            raise self._to_access_error(exc) from exc

    async def list_contacts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        company_id: UUID | None = None,
    ) -> tuple[list[dict], int]:
        """Paginated contacts with deal_count, source, and company_id annotations."""
        try:
            return await self._reader.list_contacts(
                page=page,
                limit=limit,
                q=q,
                company_id=company_id,
            )
        except ContactReadServiceError as exc:
            raise self._to_access_error(exc) from exc

    async def get_contact_detail(self, contact_id: UUID) -> dict:
        """Full contact + deal involvement via contact participants."""
        try:
            return await self._reader.get_contact_detail(contact_id)
        except ContactReadServiceError as exc:
            raise self._to_access_error(exc) from exc

    async def find_contact_by_email(self, email: str) -> CRMContact | None:
        """Return a non-deleted contact by exact case-insensitive email."""
        try:
            return await self._reader.find_contact_by_email(email)
        except ContactReadServiceError as exc:
            raise self._to_access_error(exc) from exc

    async def find_accounts_by_domain(self, domain: str) -> list[CRMCompany]:
        """Return active accounts with an exact normalized domain match."""
        return await self._account_reader.find_accounts_by_domain(domain)

    async def find_accounts_by_name(self, name: str) -> list[CRMCompany]:
        """Return active accounts with an exact case-insensitive name match."""
        return await self._account_reader.find_accounts_by_name(name)

    async def create_account(self, name: str | None, **kwargs: Any) -> CRMCompany:
        """Create an internal CRM account in the same org session as contact creation."""
        try:
            result = await self._account_writer.create_account(
                name=name,
                context=AccountWriteContext(source="contacts_access_service"),
                **kwargs,
            )
        except AccountWriteServiceError as exc:
            raise ContactsAccessServiceError(
                exc.message, status_code=exc.status_code, data=exc.data
            ) from exc
        return result.account

    async def create_contact(
        self,
        email: str,
        name: str,
        **kwargs: Any,
    ) -> CRMContact:
        """Create a contact, optionally linked to an account by explicit company_id."""
        try:
            result = await self._writer.create_contact(email=email, name=name, **kwargs)
        except ContactWriteServiceError as exc:
            raise self._to_access_error(exc) from exc
        return result.contact

    async def update_contact(self, contact_id: UUID, updates: dict) -> CRMContact:
        """Update allowed fields on a contact."""
        try:
            result = await self._writer.update_contact(contact_id, updates)
        except ContactWriteServiceError as exc:
            raise self._to_access_error(exc) from exc
        return result.contact

    async def add_contact_to_deal(
        self,
        contact_id: UUID,
        deal_id: UUID,
        relationship_role: str | None = None,
        actor_user_id: UUID | None = None,
    ) -> dict:
        """Add a contact to a deal as a participant."""
        try:
            result = await self._deal_linker.add_contact_to_deal(
                contact_id=contact_id,
                deal_id=deal_id,
                relationship_role=relationship_role,
                actor_user_id=actor_user_id,
            )
        except ContactDealLinkServiceError as exc:
            raise self._to_access_error(exc) from exc
        return self._deal_link_result_to_dict(result)

    @staticmethod
    def _deal_link_result_to_dict(result: ContactDealLinkResult) -> dict:
        return {
            "created": result.created,
            "skipped": result.skipped,
            "contact_participant_changed": result.contact_participant_changed,
        }
