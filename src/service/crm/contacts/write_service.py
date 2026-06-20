"""Write-side CRM contact service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.crm.accounts.contracts import AccountWriteServiceError
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.contacts.contracts import (
    ContactWriteContext,
    ContactWriteResult,
    ContactWriteServiceError,
)


class ContactWriteService:
    """Canonical tenant-scoped contact writer for internal CRM contact records."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Bind writes to one org-scoped session without taking commit ownership."""
        self.org_session = org_session
        self.organisation_id = organisation_id
        self._account_writer = AccountWriteService(org_session, organisation_id)

    async def close(self) -> None:
        """Close the owned org session when used as a FastAPI dependency."""
        await self.org_session.close()

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str | None]:
        """Split 'First Last' into (first, last). Last may be None for single names."""
        parts = full_name.strip().split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else None
        return first, last

    async def create_contact(
        self,
        email: str,
        name: str,
        *,
        context: ContactWriteContext | None = None,
        **kwargs: Any,
    ) -> ContactWriteResult:
        """Create an internal CRM contact with explicit-only account linking."""
        _ = context
        normalized_email = email.strip().lower()
        dup_stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(col(CRMContact.email)) == normalized_email,
        )
        dup_res = await self.org_session.execute(dup_stmt)
        if dup_res.scalars().first():
            raise ContactWriteServiceError(
                f"A contact with email '{email}' already exists",
                status_code=409,
            )

        integration = await self._get_or_create_internal_integration(context)
        first_name, last_name = self._split_name(name)

        explicit_company_id: UUID | None = kwargs.pop("company_id", None)
        company_name: str | None = kwargs.pop("company_name", None)
        properties = kwargs.pop("properties", {}) or {}
        metrics = kwargs.pop("metrics", {}) or {}
        additional_emails = kwargs.pop("additional_emails", []) or []

        if explicit_company_id:
            try:
                company = await self._account_writer.load_org_company(explicit_company_id)
            except AccountWriteServiceError as exc:
                raise ContactWriteServiceError(
                    exc.message, status_code=exc.status_code, data=exc.data
                ) from exc
            company_name = company_name or company.name

        contact = CRMContact(
            contact_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            email=normalized_email,
            first_name=first_name,
            last_name=last_name,
            full_name=name.strip(),
            company_name=company_name,
            company_id=explicit_company_id,
            synced_at=datetime.now(UTC),
            is_deleted=False,
            properties=properties,
            metrics=metrics,
            additional_emails=additional_emails,
            **kwargs,
        )

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        return ContactWriteResult(contact=contact, created=True, matched_by="created")

    async def update_contact(
        self,
        contact_id: UUID,
        updates: dict[str, Any],
        *,
        context: ContactWriteContext | None = None,
    ) -> ContactWriteResult:
        """Update allowed fields on one tenant-owned active contact."""
        _ = context
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        res = await self.org_session.execute(stmt)
        contact = res.scalars().first()

        if not contact:
            raise ContactWriteServiceError("Contact not found", status_code=404)

        new_email = updates.pop("email", None)
        if new_email is not None:
            if contact.email:
                raise ContactWriteServiceError(
                    "Cannot change an existing email address",
                    status_code=400,
                )
            normalized_email = new_email.strip().lower()
            dup_stmt = select(col(CRMContact.contact_id)).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(col(CRMContact.email)) == normalized_email,
            )
            dup_res = await self.org_session.execute(dup_stmt)
            if dup_res.scalars().first():
                raise ContactWriteServiceError(
                    f"A contact with email '{normalized_email}' already exists",
                    status_code=409,
                )
            contact.email = normalized_email

        if "company_id" in updates:
            new_company_id = updates.pop("company_id")
            if new_company_id is None:
                raise ContactWriteServiceError(
                    "Cannot unlink a contact from its account",
                    status_code=400,
                )
            if contact.company_id:
                raise ContactWriteServiceError(
                    "Cannot change the account linked to this contact",
                    status_code=400,
                )
            if not contact.email:
                raise ContactWriteServiceError(
                    "Contact must have an email address before being linked to an account. "
                    "Add an email first.",
                    status_code=400,
                )
            try:
                company = await self._account_writer.load_org_company(new_company_id)
            except AccountWriteServiceError as exc:
                raise ContactWriteServiceError(
                    exc.message, status_code=exc.status_code, data=exc.data
                ) from exc
            contact.company_id = new_company_id
            contact.company_name = company.name

        linkedin_url = updates.pop("linkedin_url", None)
        if linkedin_url is not None:
            props = dict(contact.properties or {})
            props["linkedin_url"] = linkedin_url
            contact.properties = props

        for field, value in updates.items():
            if hasattr(contact, field):
                setattr(contact, field, value)

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        return ContactWriteResult(contact=contact, created=False, matched_by="explicit_id")

    async def _get_or_create_internal_integration(
        self, context: ContactWriteContext | None = None
    ) -> KnowledgeIntegration:
        """Return the internal CRM integration used to own manually created contacts."""
        _ = context
        stmt = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == self.organisation_id,
            col(KnowledgeIntegration.integration_type) == IntegrationType.INTERNAL_CRM.value,
        )
        res = await self.org_session.execute(stmt)
        integration = res.scalars().first()
        if integration:
            return integration

        integration = KnowledgeIntegration.model_validate(
            {
                "organisation_id": self.organisation_id,
                "created_by_user_id": UUID("00000000-0000-0000-0000-000000000001"),
                "created_by_admin": False,
                "integration_type": IntegrationType.INTERNAL_CRM,
                "integration_scope": IntegrationScope.ORGANIZATION,
                "name": "Copilot CRM",
                "credentials": {},
                "connection_metadata": {"source": "internal_crm", "internal": True},
                "is_active": True,
            }
        )
        self.org_session.add(integration)
        await self.org_session.flush()
        await self.org_session.refresh(integration)
        return integration
