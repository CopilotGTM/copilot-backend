"""Contact identity resolution for email and LinkedIn-first create flows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.contacts.contracts import ContactCreateCommand, ContactWriteContext
from src.service.crm.contacts.research_service import (
    ContactProviderResearchInput,
    ContactProviderResearchResult,
    ContactResearchService,
    research_contact_with_apollo,
)
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.utils.identity import normalize_email

ContactIdentityResolutionStatus = Literal["resolved", "not_found", "error"]
ContactIdentityMatchedBy = Literal["email", "linkedin", "apollo"]
ContactIdentityProviderLookup = Callable[
    [ContactProviderResearchInput], Awaitable[ContactProviderResearchResult]
]


@dataclass(frozen=True)
class ContactIdentityResolveCommand:
    """Identity inputs for resolving one CRMContact before another flow depends on it."""

    email: str | None = None
    linkedin_url: str | None = None
    name: str | None = None
    context: ContactWriteContext | None = None


@dataclass(frozen=True)
class ContactIdentityResolutionResult:
    """Resolved contact or typed miss returned to stakeholder/account callers."""

    status: ContactIdentityResolutionStatus
    contact: CRMContact | None = None
    created: bool = False
    matched_by: ContactIdentityMatchedBy | None = None
    error_detail: str | None = None
    created_account_id: UUID | None = None


class ContactIdentityResolutionService:
    """Resolve contacts by existing CRM identity first, then Apollo provider enrichment."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        contact_writer: ContactWriteService | None = None,
        provider_lookup: ContactIdentityProviderLookup = research_contact_with_apollo,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.contact_writer = contact_writer or ContactWriteService(session, organisation_id)
        self.provider_lookup = provider_lookup

    async def resolve(
        self,
        command: ContactIdentityResolveCommand,
    ) -> ContactIdentityResolutionResult:
        """Resolve an existing contact or create one from usable Apollo identity data."""
        email = normalize_email(command.email)
        linkedin_url = _clean_optional(command.linkedin_url)
        name = _clean_optional(command.name)

        if email and (contact := await self._find_contact_by_email(email)):
            return ContactIdentityResolutionResult(
                status="resolved",
                contact=contact,
                matched_by="email",
            )

        if linkedin_url and (contact := await self._find_contact_by_linkedin_url(linkedin_url)):
            return ContactIdentityResolutionResult(
                status="resolved",
                contact=contact,
                matched_by="linkedin",
            )

        if not email and not linkedin_url:
            return ContactIdentityResolutionResult(
                status="not_found",
                error_detail="Email or LinkedIn URL is required to resolve a contact",
            )

        provider_result = await self.provider_lookup(
            ContactProviderResearchInput(
                organisation_id=self.organisation_id,
                contact_id=uuid4(),
                email=email,
                name=name,
                linkedin_url=linkedin_url,
            )
        )
        if provider_result.status != "found":
            if email:
                write_result = await self.contact_writer.find_or_create_contact_by_email(
                    email=email,
                    name=name,
                    context=command.context,
                    profile_updates={"linkedin_url": linkedin_url},
                )
                return ContactIdentityResolutionResult(
                    status="resolved",
                    contact=write_result.contact,
                    created=write_result.created,
                    matched_by="email",
                    created_account_id=write_result.created_account_id,
                )
            return ContactIdentityResolutionResult(
                status=provider_result.status,
                error_detail=_public_provider_error(
                    provider_result.status,
                    provider_result.error_detail,
                    fallback="LinkedIn research could not resolve this contact",
                ),
            )

        resolved_email = normalize_email(provider_result.email) or email
        resolved_linkedin_url = provider_result.linkedin_url or linkedin_url
        if resolved_linkedin_url and resolved_linkedin_url.lower() != (linkedin_url or "").lower():
            contact = await self._find_contact_by_linkedin_url(resolved_linkedin_url)
            if contact:
                await ContactResearchService(
                    self.session,
                    self.organisation_id,
                ).persist_provider_result(
                    contact.contact_id,
                    provider_result,
                    commit=False,
                )
                return ContactIdentityResolutionResult(
                    status="resolved",
                    contact=contact,
                    created=False,
                    matched_by="linkedin",
                )

        if not resolved_email:
            resolved_name = provider_result.name or name
            if not resolved_name and not resolved_linkedin_url:
                return ContactIdentityResolutionResult(
                    status="not_found",
                    error_detail="LinkedIn research did not provide a usable contact identity",
                )

            write_result = await self.contact_writer._create_contact_record(
                ContactCreateCommand(
                    email=None,
                    name=resolved_name,
                    job_title=provider_result.job_title,
                    linkedin_url=resolved_linkedin_url,
                ),
                context=command.context,
            )
            await ContactResearchService(
                self.session, self.organisation_id
            ).persist_provider_result(
                write_result.contact.contact_id,
                provider_result,
                commit=False,
            )
            return ContactIdentityResolutionResult(
                status="resolved",
                contact=write_result.contact,
                created=write_result.created,
                matched_by="apollo",
                created_account_id=write_result.created_account_id,
            )

        write_result = await self.contact_writer.find_or_create_contact_by_email(
            email=resolved_email,
            name=provider_result.name or name,
            context=command.context,
            profile_updates={},
        )
        await ContactResearchService(self.session, self.organisation_id).persist_provider_result(
            write_result.contact.contact_id,
            provider_result,
            commit=False,
        )
        return ContactIdentityResolutionResult(
            status="resolved",
            contact=write_result.contact,
            created=write_result.created,
            matched_by="apollo",
            created_account_id=write_result.created_account_id,
        )

    async def _find_contact_by_email(self, email: str) -> CRMContact | None:
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(func.trim(col(CRMContact.email))) == email,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _find_contact_by_linkedin_url(self, linkedin_url: str) -> CRMContact | None:
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(func.trim(col(CRMContact.linkedin_url))) == linkedin_url.lower(),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _public_provider_error(
    status: ContactIdentityResolutionStatus,
    detail: str | None,
    *,
    fallback: str,
) -> str:
    if status == "error" or (detail and "apollo" in detail.lower()):
        return fallback
    return detail or fallback
