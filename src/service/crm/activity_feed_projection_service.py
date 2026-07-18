"""Maintain compact activity-feed projections for canonical CRM source rows.

Called by CRM writers and deal-mapper workers after a source row exists. This
module keeps `crm_activities` as a read projection, while `crm_activity_links`
owns deal/contact/company feed membership for org-scoped reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
    CRMActivityType,
    CRMCompany,
    CRMContact,
    CRMDeal,
    CRMNote,
    CRMTask,
)
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMDealStageHistory,
)
from src.domain.models.organisation_tables.sync.email_models import DealEmailAssociation, Email
from src.domain.models.organisation_tables.sync.risk_blocker_models import DealRiskBlocker
from src.service.crm.deals.last_activity_service import DealLastActivityService
from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _compact_preview(value: str | None, *, limit: int = 1000) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact[:limit].rstrip() if compact else None


def note_title_or_preview(note: CRMNote, *, limit: int = 30) -> str | None:
    """Choose the feed title for a note without exposing the full body in list rows."""
    if note.title and note.title.strip():
        return note.title.strip()
    return _compact_preview(note.body, limit=limit)


def _uuid_from_metadata(metadata: dict[str, Any], key: str) -> UUID | None:
    value = metadata.get(key)
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _enum_value_required(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value)


def _participant_profile_payload(
    participant: CRMDealParticipant,
    contact: CRMContact | None,
) -> dict[str, Any]:
    return participant_to_stakeholder_detail_dict(participant, contact=contact)


def _normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _participant_preview(
    *,
    name: str | None = None,
    email: str | None,
    role: str,
) -> dict[str, str]:
    preview: dict[str, str] = {"role": role}
    if name:
        preview["name"] = name
    if email:
        preview["email"] = email
    return preview


def communication_source_activity_id(
    organisation_id: UUID,
    source_kind: str,
    source_id: object,
) -> UUID:
    """Return the stable direct-card ID shared by hydration and association cleanup."""
    return uuid5(
        NAMESPACE_URL,
        f"crm-communication:{organisation_id}:{source_kind}:{source_id}",
    )


class ActivityFeedProjectionService:
    """Upsert feed-card rows and object links from canonical CRM source tables."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def upsert_task(self, task: CRMTask) -> None:
        """Project the current task source row into the unified activity feed.

        Task writers call this after `crm_tasks` has been persisted. The projection
        is idempotent so provider replays and manual edits update the same feed card.
        """
        due_date_at = (
            datetime.combine(task.due_date, time.min, tzinfo=UTC) if task.due_date else None
        )
        occurred_at = (
            task.due_at or due_date_at or task.source_created_at or task.created_at or _now_utc()
        )
        metadata = task.task_metadata or {}
        metadata_actor_name = (
            metadata.get("actor_name") if isinstance(metadata.get("actor_name"), str) else None
        )
        metadata_actor_email = (
            metadata.get("actor_email") if isinstance(metadata.get("actor_email"), str) else None
        )
        provider = metadata.get("provider") if isinstance(metadata.get("provider"), str) else None
        await self._upsert_activity(
            activity_id=task.task_id,
            integration_id=task.integration_id,
            activity_type=CRMActivityType.TASK.value,
            title=task.title,
            summary=task.description,
            preview=_compact_preview(task.description or task.title),
            occurred_at=occurred_at,
            source_type="TASK",
            source_record_id=str(task.task_id),
            source_provider=provider,
            actor_name=metadata_actor_name or task.assigned_to_name,
            actor_email=metadata_actor_email or task.assigned_to_email or task.owner_email,
            display_payload={
                "status": task.status,
                "priority": task.priority,
                "task_type": metadata.get("hs_task_type") or metadata.get("task_type"),
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            },
        )
        await self._sync_primary_deal_link(
            activity_id=task.task_id,
            deal_id=task.deal_id,
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=task.task_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_id=_uuid_from_metadata(metadata, "contact_id"),
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=task.task_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_id=_uuid_from_metadata(metadata, "company_id"),
            occurred_at=occurred_at,
        )

    async def upsert_note(self, note: CRMNote) -> None:
        """Project the current note source row into the unified activity feed.

        Note bodies stay canonical in `crm_notes`; the feed row carries only the
        compact card fields and object links needed by list/detail readers.
        """
        occurred_at = note.source_created_at or note.created_at or _now_utc()
        metadata = note.note_metadata or {}
        provider = metadata.get("provider") if isinstance(metadata.get("provider"), str) else None
        await self._upsert_activity(
            activity_id=note.note_id,
            integration_id=note.integration_id,
            activity_type=CRMActivityType.NOTE.value,
            title=note_title_or_preview(note),
            summary=note.body,
            preview=_compact_preview(note.body),
            occurred_at=occurred_at,
            source_type="NOTE",
            source_record_id=str(note.note_id),
            source_provider=provider,
            actor_name=note.author_name,
            actor_email=note.author_email,
            display_payload={
                "body_format": note.body_format,
                "visibility": note.visibility,
                "has_attachments": bool(metadata.get("hs_attachment_ids")),
            },
        )
        await self._sync_primary_deal_link(
            activity_id=note.note_id,
            deal_id=note.deal_id,
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=note.note_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_id=_uuid_from_metadata(metadata, "contact_id"),
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=note.note_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_id=_uuid_from_metadata(metadata, "company_id"),
            occurred_at=occurred_at,
        )

    async def upsert_email_association(
        self,
        association: DealEmailAssociation,
        email: Email,
    ) -> None:
        """Project one deal-email association into one email activity card.

        The association ID is the activity identity so each deal/email link has a
        replay-safe card without making `crm_activities` the email source table.
        """
        await self._lock_communication("email", email.internet_message_id or email.email_id)
        # Once deal matching creates an association-backed card, retire the earlier direct
        # communication card so the same email is not shown twice in contact/company feeds.
        await self.soft_delete_activity_projection(
            communication_source_activity_id(
                self.organisation_id,
                "email",
                email.internet_message_id or email.email_id,
            )
        )
        occurred_at = association.email_date or email.received_at or email.sent_at or _now_utc()
        matched_emails = {
            normalized
            for raw in association.matched_stakeholder_emails or []
            if (normalized := _normalize_email(raw))
        }
        contacts = await self._resolve_contacts_by_email(matched_emails)
        company_ids = await self._resolve_company_ids(
            association.deal_id,
            contacts=contacts,
        )
        participants_preview = self._email_participants_preview(email)
        metadata = email.email_metadata or {}
        provider = metadata.get("provider") if isinstance(metadata.get("provider"), str) else None

        await self._upsert_activity(
            activity_id=association.association_id,
            integration_id=email.integration_id,
            activity_type=CRMActivityType.EMAIL.value,
            title=association.email_subject or email.subject,
            summary=email.snippet or email.body_text,
            preview=_compact_preview(email.snippet or email.body_text or email.subject),
            occurred_at=occurred_at,
            source_type="EMAIL",
            source_record_id=str(association.association_id),
            source_provider=provider,
            actor_name=email.from_name,
            actor_email=email.from_email,
            display_payload={
                "direction": _enum_value(association.direction),
                "participants_preview": participants_preview,
                "participant_count": len(self._email_participant_emails(email)),
                "has_attachments": bool(email.has_attachments),
            },
        )
        await self._sync_primary_deal_link(
            activity_id=association.association_id,
            deal_id=association.deal_id,
            occurred_at=occurred_at,
        )
        await self._sync_object_links(
            activity_id=association.association_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_ids={contact.contact_id for contact in contacts},
            link_role="RELATED",
            occurred_at=occurred_at,
        )
        await self._sync_object_links(
            activity_id=association.association_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_ids=company_ids,
            link_role="RELATED",
            occurred_at=occurred_at,
        )

    async def upsert_direct_email(
        self,
        email: Email,
        *,
        contact_ids: set[UUID],
        company_ids: set[UUID],
        replace_links: bool = True,
    ) -> bool:
        """Project an unassociated email directly into contact and company feeds.

        The communication lock and association recheck close the race with deal
        matching: an association card always wins, regardless of worker order.
        """
        source_id = email.internet_message_id or email.email_id
        await self._lock_communication("email", source_id)
        if email.internet_message_id:
            association = await self.session.execute(
                select(col(DealEmailAssociation.association_id)).where(
                    col(DealEmailAssociation.organisation_id) == self.organisation_id,
                    col(DealEmailAssociation.internet_message_id) == email.internet_message_id,
                    col(DealEmailAssociation.status) == AssociationStatus.ACTIVE.value,
                )
            )
            if association.scalar_one_or_none() is not None:
                return False

        occurred_at = email.received_at or email.sent_at or _now_utc()
        metadata = email.email_metadata or {}
        provider = metadata.get("provider") if isinstance(metadata.get("provider"), str) else None
        activity_id = communication_source_activity_id(
            self.organisation_id,
            "email",
            source_id,
        )
        await self._upsert_activity(
            activity_id=activity_id,
            integration_id=email.integration_id,
            activity_type=CRMActivityType.EMAIL.value,
            title=email.subject,
            summary=email.snippet or email.body_text,
            preview=_compact_preview(email.snippet or email.body_text or email.subject),
            occurred_at=occurred_at,
            source_type="EMAIL",
            source_record_id=str(email.email_id),
            source_provider=provider,
            actor_name=email.from_name,
            actor_email=email.from_email,
            display_payload={
                "direction": _enum_value(email.direction),
                "participants_preview": self._email_participants_preview(email),
                "participant_count": len(self._email_participant_emails(email)),
                "has_attachments": bool(email.has_attachments),
            },
        )
        await self._sync_object_links(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_ids=contact_ids,
            link_role="RELATED",
            occurred_at=occurred_at,
            replace_existing=replace_links,
        )
        await self._sync_object_links(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_ids=company_ids,
            link_role="RELATED",
            occurred_at=occurred_at,
            replace_existing=replace_links,
        )
        return True

    async def upsert_meeting_association(
        self,
        association: CalendarEventDealAssociation,
        event: CalendarEvent,
        *,
        integration_id: UUID | None = None,
    ) -> None:
        """Project one calendar-event deal association into one meeting activity card.

        Meeting association rows own the deal context. Calendar events remain the
        source for timing, organizer, and attendee display details.
        """
        await self._lock_communication("calendar", event.event_id)
        # The deal association becomes the canonical visible card. Soft-delete the stable
        # direct card first so replaying either projection cannot leave duplicate meetings.
        await self.soft_delete_activity_projection(
            communication_source_activity_id(
                self.organisation_id,
                "calendar",
                event.event_id,
            )
        )
        now = _now_utc()
        occurred_at = event.start_at if event.start_at and event.start_at <= now else None
        occurred_at = occurred_at or association.updated_at or association.created_at or now
        attendee_emails = {
            normalized
            for attendee in event.attendees or []
            if (normalized := _normalize_email(attendee.email))
        }
        contacts = await self._resolve_contacts_by_email(attendee_emails)
        company_ids = await self._resolve_company_ids(
            association.deal_id,
            contacts=contacts,
        )

        await self._upsert_activity(
            activity_id=association.association_id,
            integration_id=integration_id or getattr(event, "integration_id", None),
            activity_type=CRMActivityType.MEETING.value,
            title=event.title,
            summary=event.description,
            preview=_compact_preview(event.description or event.title),
            occurred_at=occurred_at,
            source_type="MEETING",
            source_record_id=str(association.association_id),
            source_provider=event.provider,
            actor_name=event.organiser_name,
            actor_email=event.organiser_email,
            display_payload={
                "participants_preview": self._meeting_participants_preview(event),
                "participant_count": event.attendee_count or len(event.attendees or []),
                "duration_minutes": self._meeting_duration_minutes(event),
                "meeting_status": event.status,
            },
        )
        await self._sync_primary_deal_link(
            activity_id=association.association_id,
            deal_id=association.deal_id,
            occurred_at=occurred_at,
        )
        await self._sync_object_links(
            activity_id=association.association_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_ids={contact.contact_id for contact in contacts},
            link_role="ATTENDEE",
            occurred_at=occurred_at,
        )
        await self._sync_object_links(
            activity_id=association.association_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_ids=company_ids,
            link_role="RELATED",
            occurred_at=occurred_at,
        )

    async def upsert_direct_meeting(
        self,
        event: CalendarEvent,
        *,
        integration_id: UUID | None,
        contact_ids: set[UUID],
        company_ids: set[UUID],
        replace_links: bool = True,
    ) -> bool:
        """Project an unassociated meeting directly into contact and company feeds."""
        await self._lock_communication("calendar", event.event_id)
        association = await self.session.execute(
            select(col(CalendarEventDealAssociation.association_id)).where(
                col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                col(CalendarEventDealAssociation.event_id) == event.event_id,
                col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE.value,
            )
        )
        if association.scalar_one_or_none() is not None:
            return False

        now = _now_utc()
        occurred_at = event.start_at if event.start_at and event.start_at <= now else None
        occurred_at = occurred_at or event.updated_at or event.created_at or now
        activity_id = communication_source_activity_id(
            self.organisation_id,
            "calendar",
            event.event_id,
        )
        await self._upsert_activity(
            activity_id=activity_id,
            integration_id=integration_id,
            activity_type=CRMActivityType.MEETING.value,
            title=event.title,
            summary=event.description,
            preview=_compact_preview(event.description or event.title),
            occurred_at=occurred_at,
            source_type="MEETING",
            source_record_id=str(event.event_id),
            source_provider=event.provider,
            actor_name=event.organiser_name,
            actor_email=event.organiser_email,
            display_payload={
                "participants_preview": self._meeting_participants_preview(event),
                "participant_count": event.attendee_count or len(event.attendees or []),
                "duration_minutes": self._meeting_duration_minutes(event),
                "meeting_status": event.status,
            },
        )
        await self._sync_object_links(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_ids=contact_ids,
            link_role="ATTENDEE",
            occurred_at=occurred_at,
            replace_existing=replace_links,
        )
        await self._sync_object_links(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_ids=company_ids,
            link_role="RELATED",
            occurred_at=occurred_at,
            replace_existing=replace_links,
        )
        return True

    async def sync_communication_participant_links(
        self,
        *,
        activity_id: UUID,
        contact_ids: set[UUID],
        company_ids: set[UUID],
        occurred_at: datetime,
        replace_existing: bool,
    ) -> None:
        """Sync hydration-owned links without overwriting deal-mapper-owned links."""
        for object_type, object_ids in (
            (CRMActivityObjectType.CONTACT.value, contact_ids),
            (CRMActivityObjectType.COMPANY.value, company_ids),
        ):
            await self._sync_object_links(
                activity_id=activity_id,
                object_type=object_type,
                object_ids=object_ids,
                link_role="PARTICIPANT",
                occurred_at=occurred_at,
                replace_existing=replace_existing,
            )

    async def upsert_deal_created(self, deal: CRMDeal) -> None:
        """Project a created deal row into the activity feed."""
        if deal.organisation_id != self.organisation_id:
            raise ValueError("deal does not belong to projection organisation")

        occurred_at = deal.source_created_at or deal.created_at or _now_utc()
        title = f"Deal created: {deal.name}" if deal.name else "Deal created"
        await self._upsert_activity(
            activity_id=deal.deal_id,
            integration_id=deal.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=deal.description,
            preview=_compact_preview(deal.description or deal.name),
            occurred_at=occurred_at,
            source_type="DEAL_CREATED",
            source_record_id=str(deal.deal_id),
            source_provider=None,
            actor_name=deal.owner_name,
            actor_email=deal.owner_email,
            display_payload={"deal_name": deal.name},
        )
        await self._sync_primary_deal_link(
            activity_id=deal.deal_id,
            deal_id=deal.deal_id,
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=deal.deal_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_id=deal.company_id,
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=deal.deal_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_id=deal.primary_contact_id,
            occurred_at=occurred_at,
        )
        await self._sync_owner_user_link(
            activity_id=deal.deal_id,
            user_id=deal.owner_id,
            occurred_at=occurred_at,
        )

    async def upsert_contact_created(self, contact: CRMContact) -> None:
        """Project a created contact row into the activity feed."""
        if contact.organisation_id != self.organisation_id:
            raise ValueError("contact does not belong to projection organisation")

        occurred_at = contact.source_created_at or contact.created_at or _now_utc()
        display_name = contact.full_name or contact.email
        title = f"Contact created: {display_name}" if display_name else "Contact created"
        await self._upsert_activity(
            activity_id=contact.contact_id,
            integration_id=contact.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=contact.email,
            preview=_compact_preview(contact.email or contact.full_name),
            occurred_at=occurred_at,
            source_type="CONTACT_CREATED",
            source_record_id=str(contact.contact_id),
            source_provider=None,
            actor_name=None,
            actor_email=None,
            display_payload=self._contact_display_payload(contact),
        )
        await self._sync_contact_lifecycle_links(
            activity_id=contact.contact_id,
            contact=contact,
            occurred_at=occurred_at,
        )

    async def upsert_stakeholder_added(
        self,
        participant: CRMDealParticipant,
        *,
        contact: CRMContact | None = None,
        actor_user_id: UUID | None = None,
        source: str | None = None,
        source_label: str | None = None,
        created_via: str | None = None,
    ) -> None:
        """Project a deal participant add into the unified activity feed.

        Stakeholder writes now use activities instead of legacy timeline events,
        so the participant row remains the source while links control visibility.
        """
        if participant.organisation_id != self.organisation_id:
            raise ValueError("participant does not belong to projection organisation")

        occurred_at = participant.created_at or _now_utc()
        profile = _participant_profile_payload(participant, contact)
        display_name = profile["name"] or profile["email"]
        title = f"Stakeholder added: {display_name}" if display_name else "Stakeholder added"
        contact_id = contact.contact_id if contact is not None else participant.related_local_id
        await self._upsert_activity(
            activity_id=participant.participant_id,
            integration_id=participant.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=profile["email"],
            preview=_compact_preview(profile["email"] or profile["name"]),
            occurred_at=occurred_at,
            source_type="STAKEHOLDER_ADDED",
            source_record_id=str(participant.participant_id),
            source_provider=None,
            actor_name=None,
            actor_email=None,
            display_payload={
                "participant_id": str(participant.participant_id),
                "contact_id": str(contact_id) if contact_id else None,
                "stakeholder_name": profile["name"],
                "email": profile["email"],
                "job_title": profile["job_title"],
                "participant_source": _enum_value(participant.participant_source),
                "mutation_source": source,
                "source_label": source_label,
                "created_via": created_via,
            },
        )
        await self._sync_primary_deal_link(
            activity_id=participant.participant_id,
            deal_id=participant.deal_id,
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=participant.participant_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_id=contact_id,
            occurred_at=occurred_at,
        )
        await self._sync_owner_user_link(
            activity_id=participant.participant_id,
            user_id=actor_user_id,
            occurred_at=occurred_at,
        )

    async def upsert_stakeholder_removed(
        self,
        participant: CRMDealParticipant,
        *,
        contact: CRMContact | None = None,
        actor_user_id: UUID | None = None,
        source: str | None = None,
        source_label: str | None = None,
        created_via: str | None = None,
    ) -> None:
        """Project a stakeholder removal as its own activity-feed event."""
        if participant.organisation_id != self.organisation_id:
            raise ValueError("participant does not belong to projection organisation")

        occurred_at = participant.updated_at or participant.created_at or _now_utc()
        source_record_id = f"{participant.participant_id}:STAKEHOLDER_REMOVED"
        activity_id = uuid5(
            NAMESPACE_URL,
            f"crm-stakeholder:{self.organisation_id}:{source_record_id}",
        )
        profile = _participant_profile_payload(participant, contact)
        display_name = profile["name"] or profile["email"]
        title = f"Stakeholder removed: {display_name}" if display_name else "Stakeholder removed"
        contact_id = contact.contact_id if contact is not None else participant.related_local_id
        await self._upsert_activity(
            activity_id=activity_id,
            integration_id=participant.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=profile["email"],
            preview=_compact_preview(profile["email"] or profile["name"]),
            occurred_at=occurred_at,
            source_type="STAKEHOLDER_REMOVED",
            source_record_id=source_record_id,
            source_provider=None,
            actor_name=None,
            actor_email=None,
            display_payload={
                "participant_id": str(participant.participant_id),
                "contact_id": str(contact_id) if contact_id else None,
                "stakeholder_name": profile["name"],
                "email": profile["email"],
                "job_title": profile["job_title"],
                "participant_source": _enum_value(participant.participant_source),
                "mutation_source": source,
                "source_label": source_label,
                "created_via": created_via,
            },
        )
        await self._sync_primary_deal_link(
            activity_id=activity_id,
            deal_id=participant.deal_id,
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_id=contact_id,
            occurred_at=occurred_at,
        )
        await self._sync_owner_user_link(
            activity_id=activity_id,
            user_id=actor_user_id,
            occurred_at=occurred_at,
        )

    async def upsert_stakeholder_updated(
        self,
        participant: CRMDealParticipant,
        *,
        contact: CRMContact | None = None,
        actor_user_id: UUID | None = None,
    ) -> None:
        """Project stakeholder role/profile edits into the unified activity feed."""
        if participant.organisation_id != self.organisation_id:
            raise ValueError("participant does not belong to projection organisation")

        occurred_at = participant.updated_at or _now_utc()
        source_record_id = (
            f"{participant.participant_id}:STAKEHOLDER_UPDATED:{occurred_at.isoformat()}"
        )
        activity_id = uuid5(
            NAMESPACE_URL,
            f"crm-stakeholder:{self.organisation_id}:{source_record_id}",
        )
        profile = _participant_profile_payload(participant, contact)
        display_name = profile["name"] or profile["email"]
        title = f"Stakeholder updated: {display_name}" if display_name else "Stakeholder updated"
        contact_id = contact.contact_id if contact is not None else participant.related_local_id
        await self._upsert_activity(
            activity_id=activity_id,
            integration_id=participant.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=profile["email"],
            preview=_compact_preview(profile["email"] or profile["name"]),
            occurred_at=occurred_at,
            source_type="STAKEHOLDER_UPDATED",
            source_record_id=source_record_id,
            source_provider=None,
            actor_name=None,
            actor_email=None,
            display_payload={
                "participant_id": str(participant.participant_id),
                "contact_id": str(contact_id) if contact_id else None,
                "stakeholder_name": profile["name"],
                "email": profile["email"],
                "job_title": profile["job_title"],
                "relationship_role": participant.relationship_role,
                "stance": participant.stance,
                "decision_power": participant.decision_power,
            },
        )
        await self._sync_primary_deal_link(
            activity_id=activity_id,
            deal_id=participant.deal_id,
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_id=contact_id,
            occurred_at=occurred_at,
        )
        await self._sync_owner_user_link(
            activity_id=activity_id,
            user_id=actor_user_id,
            occurred_at=occurred_at,
        )

    async def upsert_contact_enrichment_triggered(
        self,
        contact: CRMContact,
        *,
        job_id: UUID,
        actor_user_id: UUID | None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Project the start of an async contact enrichment job into the feed."""
        activity_id = self._contact_lifecycle_activity_id(
            contact.contact_id,
            "CONTACT_ENRICHMENT_TRIGGERED",
            job_id,
        )
        await self._upsert_contact_lifecycle_event(
            contact,
            activity_id=activity_id,
            source_type="CONTACT_ENRICHMENT_TRIGGERED",
            source_record_id=f"{contact.contact_id}:CONTACT_ENRICHMENT_TRIGGERED:{job_id}",
            title="Contact enrichment triggered",
            occurred_at=occurred_at or _now_utc(),
            actor_user_id=actor_user_id,
            display_payload={"job_id": str(job_id)},
        )

    async def upsert_contact_enrichment_completed(
        self,
        contact: CRMContact,
        *,
        job_id: UUID,
        provider_status: str,
        summary_status: str,
        occurred_at: datetime | None = None,
    ) -> None:
        """Project the terminal successful contact enrichment result into the feed."""
        activity_id = self._contact_lifecycle_activity_id(
            contact.contact_id,
            "CONTACT_ENRICHMENT_COMPLETED",
            job_id,
        )
        provider = contact.profile_enrichment_source
        await self._upsert_contact_lifecycle_event(
            contact,
            activity_id=activity_id,
            source_type="CONTACT_ENRICHMENT_COMPLETED",
            source_record_id=f"{contact.contact_id}:CONTACT_ENRICHMENT_COMPLETED:{job_id}",
            title="Contact enrichment completed",
            occurred_at=occurred_at or contact.profile_enriched_at or _now_utc(),
            actor_user_id=None,
            display_payload={
                "job_id": str(job_id),
                "provider_status": provider_status,
                "summary_status": summary_status,
                "provider": provider,
            },
        )

    async def upsert_account_created(self, account: CRMCompany) -> None:
        """Project a created account row into the activity feed."""
        if account.organisation_id != self.organisation_id:
            raise ValueError("account does not belong to projection organisation")

        occurred_at = account.source_created_at or account.created_at or _now_utc()
        display_name = account.name or account.domain
        title = f"Account created: {display_name}" if display_name else "Account created"
        await self._upsert_activity(
            activity_id=account.company_id,
            integration_id=account.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=account.domain,
            preview=_compact_preview(account.domain or account.name),
            occurred_at=occurred_at,
            source_type="ACCOUNT_CREATED",
            source_record_id=str(account.company_id),
            source_provider=None,
            actor_name=account.owner_name,
            actor_email=account.owner_email,
            display_payload=self._account_display_payload(account),
        )
        await self._sync_account_lifecycle_links(
            activity_id=account.company_id,
            account=account,
            occurred_at=occurred_at,
        )

    async def upsert_account_enrichment_triggered(
        self,
        account: CRMCompany,
        *,
        job_id: UUID,
        actor_user_id: UUID | None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Project the start of an async account enrichment job into the feed."""
        activity_id = self._account_lifecycle_activity_id(
            account.company_id,
            "ACCOUNT_ENRICHMENT_TRIGGERED",
            job_id,
        )
        await self._upsert_account_lifecycle_event(
            account,
            activity_id=activity_id,
            source_type="ACCOUNT_ENRICHMENT_TRIGGERED",
            source_record_id=f"{account.company_id}:ACCOUNT_ENRICHMENT_TRIGGERED:{job_id}",
            title="Account enrichment triggered",
            occurred_at=occurred_at or _now_utc(),
            actor_user_id=actor_user_id,
            display_payload={"job_id": str(job_id)},
        )

    async def upsert_account_enrichment_completed(
        self,
        account: CRMCompany,
        *,
        job_id: UUID,
        provider_status: str,
        occurred_at: datetime | None = None,
    ) -> None:
        """Project the terminal account enrichment result into the feed."""
        activity_id = self._account_lifecycle_activity_id(
            account.company_id,
            "ACCOUNT_ENRICHMENT_COMPLETED",
            job_id,
        )
        await self._upsert_account_lifecycle_event(
            account,
            activity_id=activity_id,
            source_type="ACCOUNT_ENRICHMENT_COMPLETED",
            source_record_id=f"{account.company_id}:ACCOUNT_ENRICHMENT_COMPLETED:{job_id}",
            title="Account enrichment completed",
            occurred_at=occurred_at or account.company_enriched_at or _now_utc(),
            actor_user_id=None,
            display_payload={
                "job_id": str(job_id),
                "provider_status": provider_status,
                "provider": account.company_enrichment_source,
            },
        )

    async def upsert_stage_change(
        self,
        history: CRMDealStageHistory,
        *,
        deal: CRMDeal | None = None,
    ) -> None:
        """Project one stage-history row into one stage-change activity."""
        if history.organisation_id != self.organisation_id:
            raise ValueError("stage history does not belong to projection organisation")
        if deal is not None and deal.organisation_id != self.organisation_id:
            raise ValueError("deal does not belong to projection organisation")

        deal = deal or await self._load_deal(history.deal_id)
        stage_name = deal.stage_name if deal else None
        title = f"Stage changed: {stage_name}" if stage_name else "Stage changed"
        display_payload = {
            "deal_name": deal.name if deal else None,
            "stage_name": stage_name,
            "stage_id": str(history.stage_id) if history.stage_id else None,
            "from_stage_id": str(history.from_stage_id) if history.from_stage_id else None,
        }
        await self._upsert_activity(
            activity_id=history.history_id,
            integration_id=history.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=None,
            preview=title,
            occurred_at=history.entered_at,
            source_type="STAGE_CHANGE",
            source_record_id=str(history.history_id),
            source_provider=history.source,
            actor_name=deal.owner_name if deal else None,
            actor_email=deal.owner_email if deal else None,
            display_payload=display_payload,
        )
        await self._sync_primary_deal_link(
            activity_id=history.history_id,
            deal_id=history.deal_id,
            occurred_at=history.entered_at,
        )
        await self._sync_related_object_link(
            activity_id=history.history_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_id=deal.company_id if deal else None,
            occurred_at=history.entered_at,
        )
        await self._sync_owner_user_link(
            activity_id=history.history_id,
            user_id=deal.owner_id if deal else None,
            occurred_at=history.entered_at,
        )

    async def upsert_risk_blocker_lifecycle(
        self,
        risk_blocker: DealRiskBlocker,
        *,
        source_type: str,
    ) -> None:
        """Project one risk/blocker lifecycle transition into the activity feed."""
        if risk_blocker.organisation_id != self.organisation_id:
            raise ValueError("risk/blocker does not belong to projection organisation")

        occurred_at = self._risk_blocker_occurred_at(risk_blocker, source_type)
        activity_id = self._risk_blocker_activity_id(risk_blocker, source_type, occurred_at)
        source_record_id = self._risk_blocker_source_record_id(
            risk_blocker,
            source_type,
            occurred_at,
        )
        title = self._risk_blocker_title(risk_blocker, source_type)
        actor_name = (
            risk_blocker.closed_by_user_name
            if source_type
            in {"RISK_RESOLVED", "BLOCKER_RESOLVED", "RISK_DISMISSED", "BLOCKER_DISMISSED"}
            else risk_blocker.owner_name or risk_blocker.last_modified_by_user_name
        )
        actor_email = risk_blocker.owner_email

        display_payload = {
            "risk_blocker_id": str(risk_blocker.risk_blocker_id),
            "item_type": _enum_value_required(risk_blocker.item_type),
            "severity": _enum_value_required(risk_blocker.severity),
            "category": _enum_value_required(risk_blocker.category),
            "status": _enum_value_required(risk_blocker.status),
            "source_type": _enum_value_required(risk_blocker.source_type),
            "is_ai_generated": risk_blocker.is_ai_generated,
            "owner_name": risk_blocker.owner_name,
            "owner_email": risk_blocker.owner_email,
            "resolution_notes": (
                risk_blocker.resolution_notes
                if source_type in {"RISK_RESOLVED", "BLOCKER_RESOLVED"}
                else None
            ),
        }
        await self._upsert_activity(
            activity_id=activity_id,
            integration_id=None,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=risk_blocker.description,
            preview=_compact_preview(risk_blocker.description or risk_blocker.title),
            occurred_at=occurred_at,
            source_type=source_type,
            source_record_id=source_record_id,
            source_provider=None,
            actor_name=actor_name,
            actor_email=actor_email,
            display_payload=display_payload,
        )
        await self._sync_primary_deal_link(
            activity_id=activity_id,
            deal_id=risk_blocker.deal_id,
            occurred_at=occurred_at,
        )

    async def soft_delete_risk_blocker_projection(self, risk_blocker: DealRiskBlocker) -> None:
        """Hide feed rows generated from a risk/blocker source row."""
        for activity_id in self._risk_blocker_projection_activity_ids(risk_blocker):
            await self.soft_delete_activity_projection(activity_id)

    async def soft_delete_activity_projection(self, activity_id: UUID) -> None:
        """Hide a projected activity and remove its feed memberships for this org.

        Association cleanup uses this instead of deleting source rows, so old
        matcher decisions can be replayed or audited without stale feed entries.
        """
        activity_c = cast(Any, CRMActivity).__table__.c
        link_c = cast(Any, CRMActivityLink).__table__.c
        deal_ids_stmt = select(col(CRMActivityLink.object_id)).where(
            link_c.organisation_id == self.organisation_id,
            link_c.activity_id == activity_id,
            link_c.object_type == CRMActivityObjectType.DEAL.value,
        )
        deal_ids = list((await self.session.execute(deal_ids_stmt)).scalars().all())
        await self.session.execute(
            update(CRMActivity)
            .where(
                activity_c.organisation_id == self.organisation_id,
                activity_c.activity_id == activity_id,
            )
            .values(is_deleted=True, updated_at=_now_utc())
        )
        await self.session.execute(
            delete(CRMActivityLink).where(
                link_c.organisation_id == self.organisation_id,
                link_c.activity_id == activity_id,
            )
        )
        refresher = DealLastActivityService(self.session, self.organisation_id)
        for deal_id in deal_ids:
            await refresher.refresh_deal(deal_id)

    async def _lock_communication(self, source_kind: str, source_id: object) -> None:
        """Serialize direct and association projections for one communication source."""
        # copilotgtm: hash collisions only serialize unrelated sources; switch to a
        # two-key advisory lock if projection-lock contention becomes measurable.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {
                "lock_key": (
                    f"crm-communication-projection:{self.organisation_id}:{source_kind}:{source_id}"
                )
            },
        )

    async def _upsert_activity(
        self,
        *,
        activity_id: UUID,
        integration_id: UUID | None,
        activity_type: str,
        title: str | None,
        summary: str | None,
        preview: str | None,
        occurred_at: datetime,
        source_type: str,
        source_record_id: str,
        source_provider: str | None,
        actor_name: str | None,
        actor_email: str | None,
        display_payload: dict[str, Any] | None = None,
    ) -> None:
        now = _now_utc()
        clean_display_payload = {
            key: value for key, value in (display_payload or {}).items() if value is not None
        }
        values: dict[str, Any] = {
            "activity_id": activity_id,
            "organisation_id": self.organisation_id,
            "integration_id": integration_id,
            "activity_type": activity_type,
            "title": title,
            "summary": summary,
            "preview": preview,
            "occurred_at": occurred_at,
            "source_type": source_type,
            "source_record_id": source_record_id,
            "source_provider": source_provider,
            "source_integration_id": integration_id,
            "actor_name": actor_name,
            "actor_email": actor_email,
            "display_payload": clean_display_payload,
            "is_deleted": False,
            "updated_at": now,
        }
        stmt = (
            pg_insert(CRMActivity)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["activity_id"],
                set_={
                    "integration_id": integration_id,
                    "activity_type": activity_type,
                    "title": title,
                    "summary": summary,
                    "preview": preview,
                    "occurred_at": occurred_at,
                    "source_type": source_type,
                    "source_record_id": source_record_id,
                    "source_provider": source_provider,
                    "source_integration_id": integration_id,
                    "actor_name": actor_name,
                    "actor_email": actor_email,
                    "display_payload": clean_display_payload,
                    "is_deleted": False,
                    "updated_at": now,
                },
            )
        )
        await self.session.execute(stmt)

    async def _load_deal(self, deal_id: UUID) -> CRMDeal | None:
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    def _risk_blocker_projection_activity_ids(self, risk_blocker: DealRiskBlocker) -> set[UUID]:
        activity_ids = {risk_blocker.risk_blocker_id}
        for action, source_type in {
            "resolved": self._risk_blocker_lifecycle_source_type(risk_blocker, "RESOLVED"),
            "dismissed": self._risk_blocker_lifecycle_source_type(risk_blocker, "DISMISSED"),
            "reopened": self._risk_blocker_lifecycle_source_type(risk_blocker, "REOPENED"),
        }.items():
            for timestamp in self._risk_blocker_audit_timestamps(risk_blocker, action):
                activity_ids.add(
                    self._risk_blocker_activity_id(risk_blocker, source_type, timestamp)
                )
        if risk_blocker.closed_at:
            for source_type in {
                self._risk_blocker_lifecycle_source_type(risk_blocker, "RESOLVED"),
                self._risk_blocker_lifecycle_source_type(risk_blocker, "DISMISSED"),
            }:
                activity_ids.add(
                    self._risk_blocker_activity_id(
                        risk_blocker,
                        source_type,
                        risk_blocker.closed_at,
                    )
                )
        activity_ids.add(
            self._risk_blocker_activity_id(
                risk_blocker,
                self._risk_blocker_lifecycle_source_type(risk_blocker, "REOPENED"),
                risk_blocker.updated_at,
            )
        )
        return activity_ids

    def _risk_blocker_occurred_at(
        self,
        risk_blocker: DealRiskBlocker,
        source_type: str,
    ) -> datetime:
        if source_type in {"RISK_OPENED", "BLOCKER_OPENED", "RISK_FLAGGED", "BLOCKER_ADDED"}:
            return risk_blocker.identified_at or risk_blocker.created_at or _now_utc()
        if source_type in {
            "RISK_RESOLVED",
            "BLOCKER_RESOLVED",
            "RISK_DISMISSED",
            "BLOCKER_DISMISSED",
        }:
            return risk_blocker.closed_at or risk_blocker.updated_at or _now_utc()
        if source_type in {"RISK_REOPENED", "BLOCKER_REOPENED"}:
            audit_timestamps = self._risk_blocker_audit_timestamps(risk_blocker, "reopened")
            return (
                audit_timestamps[-1] if audit_timestamps else risk_blocker.updated_at or _now_utc()
            )
        return risk_blocker.updated_at or risk_blocker.created_at or _now_utc()

    def _risk_blocker_activity_id(
        self,
        risk_blocker: DealRiskBlocker,
        source_type: str,
        occurred_at: datetime,
    ) -> UUID:
        if source_type in {"RISK_OPENED", "BLOCKER_OPENED", "RISK_FLAGGED", "BLOCKER_ADDED"}:
            return risk_blocker.risk_blocker_id
        source_record_id = self._risk_blocker_source_record_id(
            risk_blocker,
            source_type,
            occurred_at,
        )
        return uuid5(NAMESPACE_URL, f"crm-risk-blocker:{self.organisation_id}:{source_record_id}")

    @staticmethod
    def _risk_blocker_source_record_id(
        risk_blocker: DealRiskBlocker,
        source_type: str,
        occurred_at: datetime,
    ) -> str:
        if source_type in {"RISK_OPENED", "BLOCKER_OPENED", "RISK_FLAGGED", "BLOCKER_ADDED"}:
            return str(risk_blocker.risk_blocker_id)
        return f"{risk_blocker.risk_blocker_id}:{source_type}:{occurred_at.isoformat()}"

    @staticmethod
    def _risk_blocker_title(risk_blocker: DealRiskBlocker, source_type: str) -> str:
        label = {
            "RISK_OPENED": "Risk Opened",
            "BLOCKER_OPENED": "Blocker Opened",
            "RISK_FLAGGED": "Risk Opened",
            "BLOCKER_ADDED": "Blocker Opened",
            "RISK_RESOLVED": "Risk Resolved",
            "BLOCKER_RESOLVED": "Blocker Resolved",
            "RISK_DISMISSED": "Risk Dismissed",
            "BLOCKER_DISMISSED": "Blocker Dismissed",
            "RISK_REOPENED": "Risk Reopened",
            "BLOCKER_REOPENED": "Blocker Reopened",
        }.get(source_type)
        if label is None:
            item_type = _enum_value_required(risk_blocker.item_type).replace("_", " ").title()
            label = item_type or "Risk/Blocker"
        return f"{label}: {risk_blocker.title}"[:500]

    @staticmethod
    def _risk_blocker_audit_timestamps(
        risk_blocker: DealRiskBlocker,
        action: str,
    ) -> list[datetime]:
        timestamps: list[datetime] = []
        for entry in risk_blocker.audit_trail:
            if entry.get("action") != action:
                continue
            timestamp = entry.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                parsed = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
            timestamps.append(parsed)
        return timestamps

    @staticmethod
    def _risk_blocker_lifecycle_source_type(
        risk_blocker: DealRiskBlocker,
        action: str,
    ) -> str:
        item_type = _enum_value_required(risk_blocker.item_type)
        prefix = "RISK" if item_type == "RISK" else "BLOCKER"
        return f"{prefix}_{action}"

    async def _resolve_contacts_by_email(self, emails: set[str]) -> list[CRMContact]:
        if not emails:
            return []
        # Contact links are optional enrichments. The org filter is the trust
        # boundary; a matched email alone never proves ownership of a local contact.
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            func.lower(func.trim(col(CRMContact.email))).in_(emails),
            col(CRMContact.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _resolve_company_ids(
        self,
        deal_id: UUID | None,
        *,
        contacts: list[CRMContact],
    ) -> set[UUID]:
        company_ids = {contact.company_id for contact in contacts if contact.company_id}
        if deal_id is None:
            return company_ids

        # Deal company links are resolved from the local deal row under the same
        # org boundary, not inferred from email domains or provider identifiers.
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        deal = result.scalar_one_or_none()
        if deal and deal.company_id:
            company_ids.add(deal.company_id)
        return company_ids

    def _email_participant_emails(self, email: Email) -> set[str]:
        participants = {_normalize_email(email.from_email)}
        participants.update(_normalize_email(raw) for raw in email.to_emails or [])
        participants.update(_normalize_email(raw) for raw in email.cc_emails or [])
        return {participant for participant in participants if participant}

    def _email_participants_preview(self, email: Email) -> list[dict[str, str]]:
        previews: list[dict[str, str]] = []
        sender_email = _normalize_email(email.from_email)
        if sender_email:
            previews.append(
                _participant_preview(name=email.from_name, email=sender_email, role="sender")
            )
        for raw_email in [*(email.to_emails or []), *(email.cc_emails or [])]:
            recipient_email = _normalize_email(raw_email)
            if recipient_email:
                previews.append(_participant_preview(email=recipient_email, role="recipient"))
        return previews[:5]

    def _meeting_participants_preview(self, event: CalendarEvent) -> list[dict[str, str]]:
        previews: list[dict[str, str]] = []
        for attendee in event.attendees or []:
            email = _normalize_email(attendee.email)
            role = "organizer" if attendee.is_organiser else "attendee"
            previews.append(
                _participant_preview(name=attendee.display_name, email=email, role=role)
            )
        return previews[:5]

    @staticmethod
    def _meeting_duration_minutes(event: CalendarEvent) -> int | None:
        if event.start_at is None or event.end_at is None:
            return None
        return max(0, int((event.end_at - event.start_at).total_seconds() / 60))

    async def _sync_object_links(
        self,
        *,
        activity_id: UUID,
        object_type: str,
        object_ids: set[UUID],
        link_role: str,
        occurred_at: datetime,
        replace_existing: bool = True,
    ) -> None:
        link_c = cast(Any, CRMActivityLink).__table__.c
        base_where = (
            link_c.organisation_id == self.organisation_id,
            link_c.activity_id == activity_id,
            link_c.object_type == object_type,
            link_c.link_role == link_role,
        )
        if not object_ids and replace_existing:
            await self.session.execute(delete(CRMActivityLink).where(*base_where))
            return

        # Sync the full set for this role so replayed projections remove links
        # that are no longer resolvable while keeping other roles untouched.
        # Full source replays replace stale links; targeted contact backfills only add the
        # requested identity and must preserve links discovered by earlier batches.
        if replace_existing:
            await self.session.execute(
                delete(CRMActivityLink).where(*base_where, link_c.object_id.not_in(object_ids))
            )
        for object_id in object_ids:
            stmt = pg_insert(CRMActivityLink).values(
                organisation_id=self.organisation_id,
                activity_id=activity_id,
                object_type=object_type,
                object_id=object_id,
                link_role=link_role,
                link_source="SYSTEM",
                occurred_at=occurred_at,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_crm_activity_link_target_role",
                set_={"occurred_at": occurred_at},
            )
            await self.session.execute(stmt)

    async def _sync_related_object_link(
        self,
        *,
        activity_id: UUID,
        object_type: str,
        object_id: UUID | None,
        occurred_at: datetime,
    ) -> None:
        await self._sync_object_links(
            activity_id=activity_id,
            object_type=object_type,
            object_ids={object_id} if object_id else set(),
            link_role="RELATED",
            occurred_at=occurred_at,
        )

    async def _sync_owner_user_link(
        self,
        *,
        activity_id: UUID,
        user_id: UUID | None,
        occurred_at: datetime,
    ) -> None:
        await self._sync_object_links(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.USER.value,
            object_ids={user_id} if user_id else set(),
            link_role="OWNER",
            occurred_at=occurred_at,
        )

    async def _sync_primary_deal_link(
        self,
        *,
        activity_id: UUID,
        deal_id: UUID | None,
        occurred_at: datetime,
    ) -> None:
        link_c = cast(Any, CRMActivityLink).__table__.c
        base_where = (
            link_c.organisation_id == self.organisation_id,
            link_c.activity_id == activity_id,
            link_c.object_type == CRMActivityObjectType.DEAL.value,
            link_c.link_role == "PRIMARY",
        )
        if deal_id is None:
            await self.session.execute(delete(CRMActivityLink).where(*base_where))
            return

        await self.session.execute(
            delete(CRMActivityLink).where(*base_where, link_c.object_id != deal_id)
        )
        stmt = pg_insert(CRMActivityLink).values(
            organisation_id=self.organisation_id,
            activity_id=activity_id,
            object_type=CRMActivityObjectType.DEAL.value,
            object_id=deal_id,
            link_role="PRIMARY",
            link_source="SYSTEM",
            occurred_at=occurred_at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_crm_activity_link_target_role",
            set_={"occurred_at": occurred_at},
        )
        await self.session.execute(stmt)
        await DealLastActivityService(self.session, self.organisation_id).refresh_deal(deal_id)

    async def _upsert_contact_lifecycle_event(
        self,
        contact: CRMContact,
        *,
        activity_id: UUID,
        source_type: str,
        source_record_id: str,
        title: str,
        occurred_at: datetime,
        actor_user_id: UUID | None,
        display_payload: dict[str, Any],
    ) -> None:
        if contact.organisation_id != self.organisation_id:
            raise ValueError("contact does not belong to projection organisation")

        payload = {**self._contact_display_payload(contact), **display_payload}
        await self._upsert_activity(
            activity_id=activity_id,
            integration_id=contact.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=contact.email,
            preview=_compact_preview(contact.email or contact.full_name),
            occurred_at=occurred_at,
            source_type=source_type,
            source_record_id=source_record_id,
            source_provider=None,
            actor_name=None,
            actor_email=None,
            display_payload=payload,
        )
        await self._sync_contact_lifecycle_links(
            activity_id=activity_id,
            contact=contact,
            occurred_at=occurred_at,
        )
        await self._sync_owner_user_link(
            activity_id=activity_id,
            user_id=actor_user_id,
            occurred_at=occurred_at,
        )

    async def _sync_contact_lifecycle_links(
        self,
        *,
        activity_id: UUID,
        contact: CRMContact,
        occurred_at: datetime,
    ) -> None:
        await self._sync_related_object_link(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.CONTACT.value,
            object_id=contact.contact_id,
            occurred_at=occurred_at,
        )
        await self._sync_related_object_link(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_id=contact.company_id,
            occurred_at=occurred_at,
        )

    def _contact_lifecycle_activity_id(
        self,
        contact_id: UUID,
        source_type: str,
        job_id: UUID,
    ) -> UUID:
        identity = f"crm-contact:{self.organisation_id}:{contact_id}:{source_type}:{job_id}"
        return uuid5(NAMESPACE_URL, identity)

    async def _upsert_account_lifecycle_event(
        self,
        account: CRMCompany,
        *,
        activity_id: UUID,
        source_type: str,
        source_record_id: str,
        title: str,
        occurred_at: datetime,
        actor_user_id: UUID | None,
        display_payload: dict[str, Any],
    ) -> None:
        if account.organisation_id != self.organisation_id:
            raise ValueError("account does not belong to projection organisation")

        payload = {**self._account_display_payload(account), **display_payload}
        await self._upsert_activity(
            activity_id=activity_id,
            integration_id=account.integration_id,
            activity_type=CRMActivityType.EVENT.value,
            title=title,
            summary=account.domain,
            preview=_compact_preview(account.domain or account.name),
            occurred_at=occurred_at,
            source_type=source_type,
            source_record_id=source_record_id,
            source_provider=None,
            actor_name=account.owner_name,
            actor_email=account.owner_email,
            display_payload=payload,
        )
        await self._sync_account_lifecycle_links(
            activity_id=activity_id,
            account=account,
            occurred_at=occurred_at,
        )
        await self._sync_owner_user_link(
            activity_id=activity_id,
            user_id=actor_user_id,
            occurred_at=occurred_at,
        )

    async def _sync_account_lifecycle_links(
        self,
        *,
        activity_id: UUID,
        account: CRMCompany,
        occurred_at: datetime,
    ) -> None:
        await self._sync_related_object_link(
            activity_id=activity_id,
            object_type=CRMActivityObjectType.COMPANY.value,
            object_id=account.company_id,
            occurred_at=occurred_at,
        )

    def _account_lifecycle_activity_id(
        self,
        company_id: UUID,
        source_type: str,
        job_id: UUID,
    ) -> UUID:
        identity = f"crm-account:{self.organisation_id}:{company_id}:{source_type}:{job_id}"
        return uuid5(NAMESPACE_URL, identity)

    @staticmethod
    def _contact_display_payload(contact: CRMContact) -> dict[str, Any]:
        return {
            "contact_name": contact.full_name,
            "email": contact.email,
            "job_title": contact.job_title,
            "company_name": contact.company_name,
        }

    @staticmethod
    def _account_display_payload(account: CRMCompany) -> dict[str, Any]:
        return {
            "account_name": account.name,
            "domain": account.domain,
            "industry": account.industry,
            "website": account.website,
        }
