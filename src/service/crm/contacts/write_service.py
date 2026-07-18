"""Write-side CRM contact service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.activities import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealMutationEvent,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.imports import CRMImportRow
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLead,
    CRMLeadParticipant,
    CRMLeadParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.domain.models.organisation_tables.sync.participant_models import IntegrationParticipant
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.crm.accounts.contracts import AccountWriteContext, AccountWriteServiceError
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.contacts.contracts import (
    ContactArchiveImpact,
    ContactArchiveResult,
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
from src.service.crm.utils.account import (
    PERSON_LINKEDIN_URL_ERROR,
    clean_optional_string,
    is_person_linkedin_url,
    normalize_linkedin_url,
)
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)
from src.service.crm.utils.identity import is_valid_email, normalize_email
from src.service.crm.utils.person import split_full_name

ContactIdentityResolutionService = None

_CONTACT_ONLY_ACTIVITY_SOURCE_TYPES = {
    "CONTACT_CREATED",
    "CONTACT_ENRICHMENT_TRIGGERED",
    "CONTACT_ENRICHMENT_COMPLETED",
}


class ContactWriteService:
    """Canonical tenant-scoped contact writer for internal CRM contact records."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        master_session: AsyncSession | None = None,
        access_actor: CRMAccessActor | None = None,
    ):
        """Bind writes to one org-scoped session without taking commit ownership."""
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.master_session = master_session
        self.access_actor = access_actor
        self._account_writer = AccountWriteService(org_session, organisation_id, master_session)
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
        """Create or resolve an internal CRM contact from email and LinkedIn identity."""
        linkedin_url = command.linkedin_url.strip() if command.linkedin_url else None
        if linkedin_url and not is_person_linkedin_url(linkedin_url):
            raise ContactWriteServiceError(PERSON_LINKEDIN_URL_ERROR, status_code=400)

        if linkedin_url:
            resolver_cls = globals().get("ContactIdentityResolutionService")
            if resolver_cls is None:
                from src.service.crm.contacts.resolution_service import (
                    ContactIdentityResolutionService as ImportedContactIdentityResolutionService,
                )
                from src.service.crm.contacts.resolution_service import (
                    ContactIdentityResolveCommand,
                )

                resolver_cls = ImportedContactIdentityResolutionService
            else:
                from src.service.crm.contacts.resolution_service import (
                    ContactIdentityResolveCommand,
                )
            resolution = await resolver_cls(
                self.org_session,
                self.organisation_id,
                contact_writer=self,
            ).resolve(
                ContactIdentityResolveCommand(
                    email=command.email,
                    linkedin_url=linkedin_url,
                    name=command.name,
                    context=context,
                )
            )
            if resolution.status == "resolved" and resolution.contact:
                return ContactWriteResult(
                    resolution.contact,
                    resolution.created,
                    resolution.matched_by or "apollo",
                    resolution.created_account_id,
                )
            if not normalize_email(command.email):
                raise ContactWriteServiceError(
                    resolution.error_detail or "Unable to resolve LinkedIn URL to a contact",
                    status_code=404,
                )

        return await self._create_contact_record(command, context=context)

    async def resolve_or_create_local_contact(
        self,
        command: ContactCreateCommand,
        *,
        context: ContactWriteContext | None = None,
    ) -> ContactWriteResult:
        """Resolve email/LinkedIn locally or create without external enrichment.

        When both identities are present they must identify the same contact;
        choosing one would silently link a Deal to the wrong person.
        """
        email = normalize_email(command.email)
        linkedin_url = command.linkedin_url.strip() if command.linkedin_url else None
        normalized_linkedin = normalize_linkedin_url(linkedin_url)
        if linkedin_url and (len(linkedin_url) > 500 or not is_person_linkedin_url(linkedin_url)):
            raise ContactWriteServiceError(PERSON_LINKEDIN_URL_ERROR, status_code=400)
        if email and (len(email) > 320 or not is_valid_email(email)):
            raise ContactWriteServiceError("Invalid Contact email", status_code=400)

        async def _matches_for(column, value: str | None) -> list[CRMContact]:
            if not value:
                return []
            stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(func.trim(column)) == value.lower(),
            )
            result = await self.org_session.execute(stmt)
            return list(result.scalars().all())

        email_matches = await _matches_for(col(CRMContact.email), email)
        linkedin_matches: list[CRMContact] = []
        if normalized_linkedin:
            stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(
                    func.rtrim(
                        func.split_part(
                            func.split_part(
                                func.regexp_replace(
                                    func.trim(col(CRMContact.linkedin_url)),
                                    r"^(https?://)?([a-z0-9-]+\.)*linkedin\.com",
                                    "linkedin.com",
                                    "i",
                                ),
                                "?",
                                1,
                            ),
                            "#",
                            1,
                        ),
                        "/",
                    )
                )
                == normalized_linkedin,
            )
            result = await self.org_session.execute(stmt)
            linkedin_matches = list(result.scalars().all())
        if len(email_matches) > 1 or len(linkedin_matches) > 1:
            raise ContactWriteServiceError("Contact identity is ambiguous", status_code=409)
        email_contact = email_matches[0] if email_matches else None
        linkedin_contact = linkedin_matches[0] if linkedin_matches else None
        if (
            email_contact
            and linkedin_contact
            and email_contact.contact_id != linkedin_contact.contact_id
        ):
            raise ContactWriteServiceError("Contact email and LinkedIn URL conflict", 409)
        matched = email_contact or linkedin_contact
        if matched:
            return ContactWriteResult(
                matched,
                False,
                "email" if email_contact else "linkedin",
            )
        if not email and not linkedin_url:
            raise ContactWriteServiceError("Contact email or LinkedIn URL is required", 400)
        return await self._create_contact_record(command, context=context)

    async def create_unverified_local_contact(
        self,
        command: ContactCreateCommand,
        *,
        context: ContactWriteContext | None = None,
    ) -> ContactWriteResult:
        """Create a name-only local contact without attempting a weak identity match."""
        if not command.name or not command.name.strip():
            raise ContactWriteServiceError("Contact name is required", 400)
        if command.email or command.linkedin_url:
            raise ContactWriteServiceError(
                "Use contact identity resolution when email or LinkedIn URL is provided",
                400,
            )
        return await self._create_contact_record(command, context=context)

    async def _create_contact_record(
        self,
        command: ContactCreateCommand,
        *,
        context: ContactWriteContext | None = None,
    ) -> ContactWriteResult:
        """Create the CRMContact row after identity resolution has already run."""
        normalized_email = normalize_email(command.email)
        if normalized_email:
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

        account_result = None
        if command.company_id:
            try:
                account = await self._account_writer.load_org_company(command.company_id)
            except AccountWriteServiceError as exc:
                raise ContactWriteServiceError(
                    exc.message, status_code=exc.status_code, data=exc.data
                ) from exc
        else:
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
                if normalized_email
                else None
            )
            account = account_result.account if account_result else None

        integration = await InternalCRMSetupService(
            self.org_session,
            self.organisation_id,
            AUTOMATED_PIPELINE_USER_ID,
        ).get_or_create_internal_integration()
        owner_id = None
        if (
            context
            and context.actor_user_id
            and context.actor_user_id != AUTOMATED_PIPELINE_USER_ID
        ):
            owner_id = (await self._resolve_actor_crm_user(context.actor_user_id)).user_id
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
                "phone": command.phone,
                "mobile_phone": clean_optional_string(command.mobile_phone),
                "website": clean_optional_string(command.website),
                "address": clean_optional_string(command.address),
                "city": clean_optional_string(command.city),
                "state": clean_optional_string(command.state),
                "country": clean_optional_string(command.country),
                "postal_code": clean_optional_string(command.postal_code),
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

    async def _resolve_actor_crm_user(self, actor_user_id: UUID) -> CRMUser:
        try:
            return await ensure_app_user_crm_identity(
                org_session=self.org_session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                app_user_id=actor_user_id,
                actor_user_id=actor_user_id,
            )
        except CRMUserIdentityResolutionError as exc:
            raise ContactWriteServiceError(exc.message, status_code=exc.status_code) from exc

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

        return await self._create_contact_record(
            ContactCreateCommand(
                email=normalized_email,
                name=name,
                job_title=updates.get("job_title"),
                phone=updates.get("phone"),
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
            if linkedin_url and not is_person_linkedin_url(linkedin_url):
                raise ContactWriteServiceError(PERSON_LINKEDIN_URL_ERROR, status_code=400)
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

        if {"email", "linkedin_url"}.intersection(changed_fields) and (
            contact.profile_enrichment or {}
        ).get("status") == "not_found":
            # A miss belongs to the identifiers used for that attempt. Once a user adds
            # another identity, expose the contact as researchable until the retry finishes.
            contact.profile_enrichment = {}
            contact.profile_enriched_at = None
            contact.profile_enrichment_source = None

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        await self._record_contact_update_event(
            contact,
            changed_fields=changed_fields,
            context=context,
        )
        return ContactWriteResult(contact, False, "explicit_id")

    async def delete_contact(
        self,
        contact_id: UUID,
        *,
        context: ContactWriteContext | None = None,
    ) -> ContactArchiveResult:
        """Archive one internal contact and detach every active CRM reference to it."""
        contact = await self._load_active_contact(contact_id)
        await self._ensure_contact_can_be_archived(contact)
        await self._require_archive_actor(contact_id)

        now = datetime.now(UTC)
        changed_fields = {
            "is_deleted": ContactMutationPolicy.changed_field(
                "is_deleted", contact.is_deleted, True
            )
        }
        contact.is_deleted = True
        contact.synced_at = now
        contact.source_modified_at = now
        contact.last_seen_at = now
        contact.properties = {**(contact.properties or {}), "deleted_via": "api"}
        impact = await self._detach_contact_references(contact, now)

        self.org_session.add(contact)
        await self.org_session.flush()
        await self.org_session.refresh(contact)
        await self._record_contact_delete_event(
            contact,
            changed_fields={k: v for k, v in changed_fields.items() if v},
            impact=impact,
            context=context,
        )
        return ContactArchiveResult(contact=contact, impact=impact)

    async def _load_active_contact(self, contact_id: UUID) -> CRMContact:
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        result = await self.org_session.execute(stmt)
        contact = result.scalars().first()
        if not contact:
            raise ContactWriteServiceError("Contact not found", status_code=404)
        return contact

    async def _ensure_contact_can_be_archived(self, contact: CRMContact) -> None:
        internal_integration = await InternalCRMSetupService(
            self.org_session,
            self.organisation_id,
            AUTOMATED_PIPELINE_USER_ID,
        ).get_internal_integration()
        if internal_integration and contact.integration_id == internal_integration.integration_id:
            return
        raise ContactWriteServiceError(
            "This contact belongs to an external CRM and cannot be archived here.",
            status_code=409,
            data={
                "code": "external_crm_contact_read_only",
                "blocked_reason": (
                    "This contact is synced from an external CRM. Archive or delete it in "
                    "the source CRM, then sync again."
                ),
                "suggested_actions": [
                    "Open the contact in the connected CRM and archive or delete it there.",
                    "Run a CRM sync after the source record is updated.",
                ],
            },
        )

    async def _require_archive_actor(self, contact_id: UUID) -> None:
        """Restrict shared-contact archival to its creator or an administrator."""
        actor = self.access_actor
        if actor is None or actor.is_admin:
            return
        event = CRMDealMutationEvent
        stmt = select(event.event_id).where(
            col(event.organisation_id) == self.organisation_id,
            col(event.entity_type) == "contact",
            col(event.entity_id) == contact_id,
            col(event.operation) == "create_contact",
            col(event.actor_user_id) == actor.user_id,
        )
        if (await self.org_session.execute(stmt)).scalar_one_or_none() is None:
            raise ContactWriteServiceError(
                "Only the contact creator or an admin may archive it", 403
            )

    async def _detach_contact_references(
        self, contact: CRMContact, now: datetime
    ) -> ContactArchiveImpact:
        deal_primary_count = await self._clear_deal_primary_contact(contact, now)
        deal_participant_count = await self._archive_deal_participants(contact, now)
        lead_contact_count = await self._clear_lead_contact(contact, now)
        lead_participant_count = await self._archive_lead_participants(contact, now)
        integration_participant_count = await self._detach_integration_participants(contact)
        removed_links, cleared_activities, archived_activities = await self._detach_activities(
            contact, now
        )
        cancelled_jobs = await self._cancel_contact_research_jobs(contact, now)
        import_rows_retained = await self._count_import_rows_retained(contact)
        return ContactArchiveImpact(
            cleared_deal_primary_contacts=deal_primary_count,
            archived_deal_participants=deal_participant_count,
            cleared_lead_contacts=lead_contact_count,
            archived_lead_participants=lead_participant_count,
            detached_integration_participants=integration_participant_count,
            removed_contact_activity_links=removed_links,
            cleared_activity_contact_refs=cleared_activities,
            archived_contact_only_activities=archived_activities,
            cancelled_contact_research_jobs=cancelled_jobs,
            import_rows_retained=import_rows_retained,
        )

    async def _clear_deal_primary_contact(self, contact: CRMContact, now: datetime) -> int:
        result = await self.org_session.execute(
            select(CRMDeal).where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.is_deleted).is_(False),
                col(CRMDeal.primary_contact_id) == contact.contact_id,
            )
        )
        deals = result.scalars().all()
        for deal in deals:
            deal.primary_contact_id = None
            deal.primary_contact_name = None
            deal.primary_contact_email = None
            deal.synced_at = now
            deal.source_modified_at = now
            self.org_session.add(deal)
        return len(deals)

    async def _archive_deal_participants(self, contact: CRMContact, now: datetime) -> int:
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            self._local_id_or_email_match(
                CRMDealParticipant.related_local_id,
                CRMDealParticipant.related_email,
                contact,
            ),
        )
        result = await self.org_session.execute(stmt)
        participants = result.scalars().all()
        for participant in participants:
            participant.is_deleted = True
            participant.synced_at = now
            participant.source_modified_at = now
            participant.last_seen_at = now
            self.org_session.add(participant)
        return len(participants)

    async def _clear_lead_contact(self, contact: CRMContact, now: datetime) -> int:
        result = await self.org_session.execute(
            select(CRMLead).where(
                col(CRMLead.organisation_id) == self.organisation_id,
                col(CRMLead.is_deleted).is_(False),
                col(CRMLead.contact_id) == contact.contact_id,
            )
        )
        leads = result.scalars().all()
        for lead in leads:
            lead.contact_id = None
            lead.synced_at = now
            lead.source_modified_at = now
            lead.last_seen_at = now
            self.org_session.add(lead)
        return len(leads)

    async def _archive_lead_participants(self, contact: CRMContact, now: datetime) -> int:
        stmt = select(CRMLeadParticipant).where(
            col(CRMLeadParticipant.organisation_id) == self.organisation_id,
            col(CRMLeadParticipant.is_deleted).is_(False),
            col(CRMLeadParticipant.participant_type) == CRMLeadParticipantType.CONTACT.value,
            self._local_id_or_email_match(
                CRMLeadParticipant.related_local_id,
                CRMLeadParticipant.email,
                contact,
            ),
        )
        result = await self.org_session.execute(stmt)
        participants = result.scalars().all()
        for participant in participants:
            participant.is_deleted = True
            participant.synced_at = now
            participant.source_modified_at = now
            participant.last_seen_at = now
            self.org_session.add(participant)
        return len(participants)

    async def _detach_integration_participants(self, contact: CRMContact) -> int:
        result = await self.org_session.execute(
            select(IntegrationParticipant).where(
                col(IntegrationParticipant.organisation_id) == self.organisation_id,
                col(IntegrationParticipant.crm_contact_id) == contact.contact_id,
            )
        )
        participants = result.scalars().all()
        for participant in participants:
            participant.crm_contact_id = None
            participant.resolution_status = "unresolved"
            participant.resolution_method = None
            participant.resolution_confidence = None
            participant.last_resolved_at = None
            self.org_session.add(participant)
        return len(participants)

    async def _detach_activities(self, contact: CRMContact, now: datetime) -> tuple[int, int, int]:
        removed_links = await self._remove_contact_activity_links(contact)
        cleared_activity_refs = await self._clear_activity_contact_refs(contact, now)
        archived_contact_only = await self._archive_contact_only_activities(contact)
        return removed_links, cleared_activity_refs, archived_contact_only

    async def _archive_contact_only_activities(self, contact: CRMContact) -> int:
        source_record_prefix = f"{contact.contact_id}:CONTACT_ENRICHMENT_"
        result = await self.org_session.execute(
            select(col(CRMActivity.activity_id)).where(
                col(CRMActivity.organisation_id) == self.organisation_id,
                or_(col(CRMActivity.is_deleted).is_(False), col(CRMActivity.is_deleted).is_(None)),
                and_(
                    col(CRMActivity.source_type).in_(_CONTACT_ONLY_ACTIVITY_SOURCE_TYPES),
                    or_(
                        col(CRMActivity.source_record_id) == str(contact.contact_id),
                        col(CRMActivity.source_record_id).startswith(source_record_prefix),
                    ),
                ),
            )
        )
        activity_ids = result.scalars().all()
        for activity_id in activity_ids:
            await self._projection.soft_delete_activity_projection(activity_id)
        return len(activity_ids)

    async def _clear_activity_contact_refs(self, contact: CRMContact, now: datetime) -> int:
        contact_ref_matches = col(CRMActivity.contact_id) == contact.contact_id
        related_contact_matches = (
            col(CRMActivity.related_object_type) == CRMActivityObjectType.CONTACT.value
        ) & (col(CRMActivity.related_local_id) == contact.contact_id)
        result = await self.org_session.execute(
            update(CRMActivity)
            .where(
                col(CRMActivity.organisation_id) == self.organisation_id,
                or_(col(CRMActivity.is_deleted).is_(False), col(CRMActivity.is_deleted).is_(None)),
                or_(contact_ref_matches, related_contact_matches),
            )
            .values(
                contact_id=case(
                    (contact_ref_matches, None),
                    else_=col(CRMActivity.contact_id),
                ),
                related_local_id=case(
                    (related_contact_matches, None),
                    else_=col(CRMActivity.related_local_id),
                ),
                updated_at=now,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def _remove_contact_activity_links(self, contact: CRMContact) -> int:
        result = await self.org_session.execute(
            select(CRMActivityLink).where(
                col(CRMActivityLink.organisation_id) == self.organisation_id,
                col(CRMActivityLink.object_type) == CRMActivityObjectType.CONTACT.value,
                col(CRMActivityLink.object_id) == contact.contact_id,
            )
        )
        links = result.scalars().all()
        for link in links:
            await self.org_session.delete(link)
        return len(links)

    async def _cancel_contact_research_jobs(self, contact: CRMContact, now: datetime) -> int:
        result = await self.org_session.execute(
            select(DealJobs).where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.job_type) == DealJobType.CONTACT_RESEARCH.value,
                col(DealJobs.status).in_([JobStatus.PENDING.value, JobStatus.IN_PROGRESS.value]),
            )
        )
        jobs = [
            job
            for job in result.scalars().all()
            if str((job.job_metadata or {}).get("contact_id")) == str(contact.contact_id)
        ]
        for job in jobs:
            job.status = JobStatus.CANCELLED
            job.error_message = "Contact archived before research completed"
            job.job_metadata = {**(job.job_metadata or {}), "cancelled_via": "contact_archive"}
            job.updated_at = now
            self.org_session.add(job)
        return len(jobs)

    async def _count_import_rows_retained(self, contact: CRMContact) -> int:
        result = await self.org_session.execute(
            select(func.count(CRMImportRow.row_id)).where(
                col(CRMImportRow.organisation_id) == self.organisation_id,
                col(CRMImportRow.contact_id) == contact.contact_id,
            )
        )
        return int(result.scalar_one() or 0)

    @staticmethod
    def _local_id_or_email_match(local_id_column: Any, email_column: Any, contact: CRMContact):
        normalized_email = normalize_email(contact.email)
        if normalized_email:
            return or_(
                local_id_column == contact.contact_id,
                func.lower(func.trim(email_column)) == normalized_email,
            )
        return local_id_column == contact.contact_id

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

    async def _record_contact_delete_event(
        self,
        contact: CRMContact,
        *,
        changed_fields: dict[str, dict[str, Any]],
        impact: ContactArchiveImpact,
        context: ContactWriteContext | None,
    ) -> None:
        command = CRMMutationRecordCommand(
            entity_type="contact",
            entity_id=contact.contact_id,
            event_type="crm.contact.deleted",
            operation="archive_contact",
            changed_fields=changed_fields,
            semantic_event_types=["crm.contact.deleted"],
            context=self._mutation_context(context),
            summary=(
                "Contact archived and detached: "
                f"{contact.full_name or contact.email or contact.contact_id}; "
                f"impact={impact}"
            ),
            integration_id=contact.integration_id,
            idempotency_scope=f"contact:archive:{contact.contact_id}",
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
