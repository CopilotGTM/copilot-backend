"""Privacy-bounded transcript and summary event production."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus, User
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
from src.service.automation.events.contracts import AutomationEventEnvelope


@dataclass(frozen=True)
class MeetingEventContext:
    """Event-time participant and related-record identifiers safe to snapshot."""

    owner_id: UUID | None = None
    attendee_ids: tuple[UUID, ...] = ()
    team_ids: tuple[str, ...] = ()
    deal_id: UUID | None = None


class MeetingEventAdapter:
    """Build content-free envelopes from durable meeting state transitions."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    def transcript_ready(
        self,
        meeting: Meeting,
        *,
        context: MeetingEventContext,
    ) -> AutomationEventEnvelope:
        """Apply the transcript ready contract for this organisation-scoped module."""
        if _value(meeting.status) != MeetingStatus.READY.value or not meeting.full_transcript:
            raise ValueError("Meeting transcript is not ready")
        if meeting.transcript_fetched_at is None:
            raise ValueError("Meeting transcript ready time is missing")
        return self._ready_event(
            meeting,
            event_key="meeting.transcript.ready",
            source_fact_id=self.transcript_source_event_id(meeting),
            occurred_at=meeting.transcript_fetched_at,
            context=context,
            actor_type="system",
            actor_id=None,
        )

    @staticmethod
    def transcript_source_event_id(meeting: Meeting) -> str:
        """Return the most specific durable provider identity available."""

        for key in ("transcript_id", "recording_id"):
            value = meeting.source_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # copilotgtm: Providers without revision IDs produce one event per meeting;
        # add a persisted transcript revision before supporting replacements.
        return str(meeting.meeting_id)

    def summary_ready(
        self,
        meeting: Meeting,
        *,
        summary_fact_id: UUID,
        context: MeetingEventContext,
    ) -> AutomationEventEnvelope:
        """Apply the summary ready contract for this organisation-scoped module."""
        if (
            _value(meeting.meeting_summary_status) != GenerationStatus.COMPLETED.value
            or not meeting.meeting_summary_data
        ):
            raise ValueError("Meeting summary is not ready")
        if meeting.meeting_summary_generated_at is None:
            raise ValueError("Meeting summary ready time is missing")
        return self._ready_event(
            meeting,
            event_key="meeting.summary.ready",
            source_fact_id=summary_fact_id,
            occurred_at=meeting.meeting_summary_generated_at,
            context=context,
            actor_type="system",
            actor_id=None,
        )

    def _ready_event(
        self,
        meeting: Meeting,
        *,
        event_key: str,
        source_fact_id: UUID | str,
        occurred_at: datetime,
        context: MeetingEventContext,
        actor_type: str,
        actor_id: str | None,
    ) -> AutomationEventEnvelope:
        if meeting.organisation_id != self.organisation_id:
            raise ValueError("Meeting producer boundary does not match organisation")
        if meeting.is_deleted:
            raise ValueError("Deleted meetings cannot produce ready events")
        provider = _value(meeting.source)
        event_id = uuid5(
            NAMESPACE_URL,
            f"copilotgtm:{self.organisation_id}:{event_key}:{source_fact_id}",
        )
        return AutomationEventEnvelope.model_validate(
            {
                "event_id": event_id,
                "organisation_id": self.organisation_id,
                "event_key": event_key,
                "route": {"mode": "match"},
                "occurred_at": _utc(occurred_at),
                "source": {
                    "domain": "meeting",
                    "system": provider,
                    "operation": "signal",
                    "mode": "system",
                    "source_event_id": str(source_fact_id),
                },
                "actor": {"type": actor_type, "id": actor_id},
                "subject": {"type": "meeting", "id": str(meeting.meeting_id)},
                "record": {
                    "id": str(meeting.meeting_id),
                    "provider": provider,
                    "language": meeting.transcript_language,
                    "owner_id": str(context.owner_id) if context.owner_id else None,
                    "team_ids": list(context.team_ids),
                    "attendee_ids": [str(value) for value in context.attendee_ids],
                    "deal_id": str(context.deal_id) if context.deal_id else None,
                },
                "references": {"meeting_id": str(meeting.meeting_id)},
                "audience_user_ids": list(
                    dict.fromkeys(
                        ([context.owner_id] if context.owner_id else [])
                        + list(context.attendee_ids)
                    )
                ),
                "correlation_id": str(meeting.calendar_event_id),
                "idempotency_key": f"{event_key}:{source_fact_id}",
            }
        )


async def load_meeting_event_context(
    session: AsyncSession,
    organisation_id: UUID,
    meeting: Meeting,
    *,
    deal_id: UUID | None = None,
) -> MeetingEventContext:
    """Resolve stable app-user and CRM-team identifiers for one calendar event."""

    if meeting.organisation_id != organisation_id:
        raise ValueError("Meeting context boundary does not match organisation")
    event_columns = cast(Any, CalendarEvent).__table__.c
    event = (
        (
            await session.execute(
                select(CalendarEvent).where(
                    event_columns.organisation_id == organisation_id,
                    event_columns.event_id == meeting.calendar_event_id,
                    event_columns.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .first()
    )
    if event is None:
        raise ValueError("Meeting calendar event is missing")

    attendee_columns = cast(Any, CalendarAttendee).__table__.c
    attendee_emails = {
        str(value).strip().casefold()
        for value in (
            await session.execute(
                select(attendee_columns.email).where(
                    attendee_columns.organisation_id == organisation_id,
                    attendee_columns.event_id == meeting.calendar_event_id,
                )
            )
        ).scalars()
        if value and str(value).strip()
    }
    organiser_email = event.organiser_email.strip().casefold() if event.organiser_email else None
    participant_emails = attendee_emails | ({organiser_email} if organiser_email else set())

    user_columns = cast(Any, User).__table__.c
    user_rows: list[Any] = []
    if participant_emails:
        user_rows = list(
            (
                await session.execute(
                    select(user_columns.user_id, user_columns.email).where(
                        user_columns.organisation_id == organisation_id,
                        user_columns.status == SupabaseUserStatus.ACTIVE.value,
                        user_columns.admin_status == AdminUserStatus.APPROVED.value,
                        func.lower(func.trim(user_columns.email)).in_(participant_emails),
                    )
                )
            ).all()
        )
    users_by_email = {
        str(row.email).strip().casefold(): row.user_id for row in user_rows if row.email
    }
    owner_id = users_by_email.get(organiser_email) if organiser_email else None

    if owner_id is None:
        state_columns = cast(Any, CalendarEventUserState).__table__.c
        integration_columns = cast(Any, KnowledgeIntegration).__table__.c
        integration_owner_ids = list(
            (
                await session.execute(
                    select(integration_columns.created_by_user_id)
                    .join(
                        CalendarEventUserState,
                        state_columns.integration_id == integration_columns.integration_id,
                    )
                    .where(
                        state_columns.organisation_id == organisation_id,
                        state_columns.event_id == meeting.calendar_event_id,
                        state_columns.is_deleted.is_(False),
                        integration_columns.organisation_id == organisation_id,
                        integration_columns.is_active.is_(True),
                    )
                    .order_by(integration_columns.created_by_user_id)
                )
            ).scalars()
        )
        if integration_owner_ids:
            owner_id = (
                (
                    await session.execute(
                        select(user_columns.user_id)
                        .where(
                            user_columns.organisation_id == organisation_id,
                            user_columns.user_id.in_(integration_owner_ids),
                            user_columns.status == SupabaseUserStatus.ACTIVE.value,
                            user_columns.admin_status == AdminUserStatus.APPROVED.value,
                        )
                        .order_by(user_columns.user_id)
                    )
                )
                .scalars()
                .first()
            )

    attendee_ids = {row.user_id for row in user_rows}
    if owner_id:
        attendee_ids.add(owner_id)

    team_ids: set[str] = set()
    if participant_emails:
        crm_user_columns = cast(Any, CRMUser).__table__.c
        crm_users = (
            (
                await session.execute(
                    select(CRMUser).where(
                        crm_user_columns.organisation_id == organisation_id,
                        crm_user_columns.is_deleted.is_(False),
                        crm_user_columns.is_active.is_(True),
                        func.lower(func.trim(crm_user_columns.email)).in_(participant_emails),
                    )
                )
            )
            .scalars()
            .all()
        )
        for crm_user in crm_users:
            team_ids.update(value for value in crm_user.team_ids if value)

    if deal_id is None:
        association_columns = cast(Any, CalendarEventDealAssociation).__table__.c
        deal_ids = list(
            (
                await session.execute(
                    select(association_columns.deal_id).where(
                        association_columns.organisation_id == organisation_id,
                        association_columns.event_id == meeting.calendar_event_id,
                        association_columns.status == AssociationStatus.ACTIVE.value,
                    )
                )
            ).scalars()
        )
        if len(deal_ids) == 1:
            deal_id = deal_ids[0]

    return MeetingEventContext(
        owner_id=owner_id,
        attendee_ids=tuple(sorted(attendee_ids, key=str)),
        team_ids=tuple(sorted(team_ids)),
        deal_id=deal_id,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _value(value: object) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


__all__ = [
    "MeetingEventAdapter",
    "MeetingEventContext",
    "load_meeting_event_context",
]
