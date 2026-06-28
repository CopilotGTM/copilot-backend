"""Write-side CRM contact service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.service.crm.accounts.contracts import AccountWriteContext, AccountWriteServiceError
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.contacts.contracts import (
    ContactCreateCommand,
    ContactWriteContext,
    ContactWriteResult,
    ContactWriteServiceError,
)
from src.service.crm.internal.internal_crm_setup_service import (
    AUTOMATED_PIPELINE_USER_ID,
    InternalCRMSetupService,
)
from src.service.crm.mutations.contracts import (
    CRMMutationContext,
    CRMMutationEventServiceError,
    CRMMutationRecordCommand,
)
from src.service.crm.mutations.event_service import CRMMutationEventService
from src.service.crm.mutations.policies import ContactMutationPolicy
from src.service.crm.utils.identity import normalize_email
from src.service.crm.utils.person import split_full_name


class ContactWriteService:
    """Canonical tenant-scoped contact writer for internal CRM contact records."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Bind writes to one org-scoped session without taking commit ownership."""
        self.org_session = org_session
        self.organisation_id = organisation_id
        self._account_writer = AccountWriteService(org_session, organisation_id)
        self._mutation_events = CRMMutationEventService(org_session, organisation_id)
        self._projection = ActivityFeedProjectionService(org_session, organisation_id)

    async def close(self) -> None:
        """Close the owned org session when used as a FastAPI dependency."""
        await self.org_session.close()

    async def create_contact(
        self,
        command: ContactCreateCommand,
        *,
        context: ContactWriteContext | None = None,
    ) -> ContactWriteResult:
        """Create an internal CRM contact without account-link side effects."""
        normalized_email = normalize_email(command.email) or ""
        dup_stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(col(CRMContact.email)) == normalized_email,
        )
        dup_res = await self.org_session.execute(dup_stmt)
        if dup_res.scalars().first():
            raise ContactWriteServiceError(
                f"A contact with email '{command.email}' already exists",
                status_code=409,
            )

        integration = await InternalCRMSetupService(
            self.org_session,
            self.organisation_id,
            AUTOMATED_PIPELINE_USER_ID,
        ).get_or_create_internal_integration()
        owner_id = (
            context.actor_user_id
            if context and context.actor_user_id != AUTOMATED_PIPELINE_USER_ID
            else None
        )
        owner_name = (
            context.actor_name.strip()
            if owner_id and context and context.actor_name and context.actor_name.strip()
            else None
        )
        owner_email = (
            context.actor_email.strip()
            if owner_id and context and context.actor_email and context.actor_email.strip()
            else None
        )
        contact_name = command.name.strip() if command.name and command.name.strip() else None
        first_name, last_name = split_full_name(contact_name) if contact_name else (None, None)
        account_result = (
            await self._account_writer.resolve_or_create_account_for_contact_email_with_result(
                normalized_email,
                context=AccountWriteContext(
                    source=context.source if context else "contact_create",
                    actor_user_id=context.actor_user_id if context else None,
                    actor_name=context.actor_name if context else None,
                    actor_email=context.actor_email if context else None,
                    source_label=context.source_label if context else None,
                    created_via="contact_create",
                ),
            )
        )
        account = account_result.account if account_result else None

        contact = CRMContact.model_validate(
            {
                "contact_id": uuid4(),
                "organisation_id": self.organisation_id,
                "integration_id": integration.integration_id,
                "email": normalized_email,
                "first_name": first_name,
                "last_name": last_name,
                "full_name": contact_name,
                "job_title": command.job_title,
                "linkedin_url": command.linkedin_url,
                "company_id": account.company_id if account else None,
                "company_name": account.name if account else None,
                "owner_id": owner_id,
                "owner_name": owner_name,
                "owner_email": owner_email,
                "synced_at": datetime.now(UTC),
                "is_deleted": False,
            }
        )

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        await self._projection.upsert_contact_created(contact)
        await self._record_contact_create_event(contact, context=context)
        created_account_id = (
            account.company_id if account and account_result and account_result.created else None
        )
        return ContactWriteResult(contact, True, "created", created_account_id)

    async def find_or_create_contact_by_email(
        self,
        *,
        email: str,
        name: str | None,
        context: ContactWriteContext | None = None,
        profile_updates: dict[str, Any] | None = None,
    ) -> ContactWriteResult:
        """Find by normalized email or create one internal CRM contact.

        Stakeholder, agent, workflow, and form callers use this path so contact
        normalization and future contact mutation events stay in one service.
        """
        normalized_email = normalize_email(email) or ""
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(col(CRMContact.email)) == normalized_email,
        )
        result = await self.org_session.execute(stmt)
        existing = result.scalars().first()
        updates = dict(profile_updates or {})

        if existing:
            changed_fields: dict[str, dict[str, Any]] = {}
            if name:
                stripped_name = name.strip()
                if change := ContactMutationPolicy.changed_field(
                    "full_name", existing.full_name, stripped_name
                ):
                    changed_fields["full_name"] = change
                existing.full_name = stripped_name
                first_name, last_name = split_full_name(name)
                if change := ContactMutationPolicy.changed_field(
                    "first_name", existing.first_name, first_name
                ):
                    changed_fields["first_name"] = change
                if change := ContactMutationPolicy.changed_field(
                    "last_name", existing.last_name, last_name
                ):
                    changed_fields["last_name"] = change
                existing.first_name = first_name
                existing.last_name = last_name

            properties = updates.pop("properties", None)
            if isinstance(properties, dict):
                linkedin_url = properties.pop("linkedin_url", None)
                if linkedin_url is not None:
                    if change := ContactMutationPolicy.changed_field(
                        "linkedin_url", existing.linkedin_url, linkedin_url
                    ):
                        changed_fields["linkedin_url"] = change
                    existing.linkedin_url = linkedin_url
                existing.properties = {**(existing.properties or {}), **properties}

            for field, value in updates.items():
                if hasattr(existing, field):
                    if change := ContactMutationPolicy.changed_field(
                        field, getattr(existing, field), value
                    ):
                        changed_fields[field] = change
                    setattr(existing, field, value)

            self.org_session.add(existing)
            await self.org_session.flush()
            await self.org_session.refresh(existing)
            await self._record_contact_update_event(
                existing,
                changed_fields=changed_fields,
                context=context,
            )
            return ContactWriteResult(existing, False, "email")

        return await self.create_contact(
            ContactCreateCommand(
                email=normalized_email,
                name=name,
                job_title=updates.get("job_title"),
                linkedin_url=(
                    updates.get("linkedin_url")
                    or (
                        updates.get("properties", {}).get("linkedin_url")
                        if isinstance(updates.get("properties"), dict)
                        else None
                    )
                ),
            ),
            context=context,
        )

    async def update_contact(
        self,
        contact_id: UUID,
        updates: dict[str, Any],
        *,
        context: ContactWriteContext | None = None,
    ) -> ContactWriteResult:
        """Update allowed fields on one tenant-owned active contact."""
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        res = await self.org_session.execute(stmt)
        contact = res.scalars().first()

        if not contact:
            raise ContactWriteServiceError("Contact not found", status_code=404)

        changed_fields: dict[str, dict[str, Any]] = {}

        new_email = updates.pop("email", None)
        if new_email is not None:
            if contact.email:
                raise ContactWriteServiceError(
                    "Cannot change an existing email address",
                    status_code=400,
                )
            normalized_email = normalize_email(new_email) or ""
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
            if change := ContactMutationPolicy.changed_field(
                "email", contact.email, normalized_email
            ):
                changed_fields["email"] = change
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
            if change := ContactMutationPolicy.changed_field(
                "company_id", contact.company_id, new_company_id
            ):
                changed_fields["company_id"] = change
            if change := ContactMutationPolicy.changed_field(
                "company_name", contact.company_name, company.name
            ):
                changed_fields["company_name"] = change
            contact.company_id = new_company_id
            contact.company_name = company.name

        if "linkedin_url" in updates:
            linkedin_url = updates.pop("linkedin_url")
            if contact.linkedin_url and linkedin_url != contact.linkedin_url:
                raise ContactWriteServiceError(
                    "Cannot change an existing LinkedIn URL",
                    status_code=400,
                )
            if change := ContactMutationPolicy.changed_field(
                "linkedin_url", contact.linkedin_url, linkedin_url
            ):
                changed_fields["linkedin_url"] = change
            contact.linkedin_url = linkedin_url

        for field, value in updates.items():
            if hasattr(contact, field):
                if change := ContactMutationPolicy.changed_field(
                    field, getattr(contact, field), value
                ):
                    changed_fields[field] = change
                setattr(contact, field, value)

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        await self._record_contact_update_event(
            contact,
            changed_fields=changed_fields,
            context=context,
        )
        return ContactWriteResult(contact, False, "explicit_id")

    async def _record_contact_create_event(
        self,
        contact: CRMContact,
        *,
        context: ContactWriteContext | None,
    ) -> None:
        changed_fields = ContactMutationPolicy.build_create_changed_fields(contact)
        command = CRMMutationRecordCommand(
            entity_type="contact",
            entity_id=contact.contact_id,
            event_type="crm.contact.created",
            operation="create_contact",
            changed_fields=changed_fields,
            semantic_event_types=ContactMutationPolicy.semantic_for_create(),
            context=self._mutation_context(context),
            summary=f"Contact created: {contact.full_name or contact.email or contact.contact_id}",
            integration_id=contact.integration_id,
            idempotency_scope=f"contact:create:{contact.contact_id}",
        )
        await self._record_mutation_event(command)

    async def _record_contact_update_event(
        self,
        contact: CRMContact,
        *,
        changed_fields: dict[str, dict[str, Any]],
        context: ContactWriteContext | None,
    ) -> None:
        command = CRMMutationRecordCommand(
            entity_type="contact",
            entity_id=contact.contact_id,
            event_type="crm.contact.updated",
            operation="update_contact",
            changed_fields=changed_fields,
            semantic_event_types=ContactMutationPolicy.semantic_for_update(changed_fields),
            context=self._mutation_context(context),
            summary=f"Contact updated: {contact.full_name or contact.email or contact.contact_id}",
            integration_id=contact.integration_id,
            idempotency_scope=f"contact:update:{contact.contact_id}",
        )
        await self._record_mutation_event(command)

    async def _record_mutation_event(self, command: CRMMutationRecordCommand) -> None:
        try:
            await self._mutation_events.record_event(command)
        except CRMMutationEventServiceError as exc:
            raise ContactWriteServiceError(
                exc.message,
                status_code=exc.status_code,
                data=exc.data,
            ) from exc

    @staticmethod
    def _mutation_context(context: ContactWriteContext | None) -> CRMMutationContext:
        if context is None:
            return CRMMutationContext(source="api")
        return CRMMutationContext(
            source=context.source,
            actor_user_id=context.actor_user_id,
            source_label=context.source_label,
        )
