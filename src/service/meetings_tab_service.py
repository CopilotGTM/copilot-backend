"""Service layer for the top-level Meetings tab.

Called by `/users/meetings` routes. The service reuses calendar sync, deal
meeting associations, CalendarInviteService, and Recall recording sessions
instead of creating a parallel meeting system.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
from sqlmodel import col
from src.api.schemas.meetings_tab_models import (
    CreateMeetingRequest,
    CreateMeetingResponse,
    MeetingAttendeeItem,
    MeetingBotPreferenceValue,
    MeetingBotStatus,
    MeetingDetail,
    MeetingLinkedRecord,
    MeetingLinkedRecordType,
    MeetingListItem,
    MeetingRecordingResponse,
    MeetingsPage,
    MeetingsView,
)
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.leads import CRMLead, CRMLeadParticipant
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (
    Meeting,
    MeetingStatus,
)
from src.domain.models.organisation_tables.sync.meeting_links import (
    MeetingBotPreference,
    MeetingBotPreferenceOverride,
    MeetingRecordLink,
    MeetingRecordLinkType,
)
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
)
from src.infra.config.configs import recall_config
from src.infra.utils.logger import get_logger
from src.service.calendar_invites.models import CalendarInviteAttendee, CalendarInviteCreateRequest
from src.service.calendar_invites.service import CalendarInviteService
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name
from src.service.integrations.meeting_recording.providers import RecallMeetingRecordingProvider
from src.service.meeting_bot.dispatch_service import MeetingRecordingDispatchService

logger = get_logger(__name__)

CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)


class MeetingsTabServiceError(Exception):
    """Error type surfaced by the Meetings tab router."""

    def __init__(self, message: str, status_code: int = 500, *, code: str | None = None):
        self.message = message
        self.status_code = status_code
        self.code = code
        super().__init__(message)


class MeetingsTabService:
    """Read and write operations for current-user Meetings V1."""

    def __init__(
        self,
        session: AsyncSession,
        master_session: AsyncSession,
        *,
        organisation_id: UUID,
        user_id: UUID,
        user_timezone: str | None = None,
    ) -> None:
        self.session = session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.user_timezone = user_timezone or "UTC"

    async def close(self) -> None:
        """Close sessions owned by FastAPI dependencies."""
        try:
            await self.session.close()
        except Exception:
            logger.debug("MeetingsTabService org session close failed", exc_info=True)
        try:
            await self.master_session.close()
        except Exception:
            logger.debug("MeetingsTabService master session close failed", exc_info=True)

    async def list_meetings(
        self,
        *,
        view: MeetingsView = MeetingsView.TODAY,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        q: str | None = None,
        linked_type: MeetingLinkedRecordType | None = None,
        bot_status: str | None = None,
        recording_status: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> MeetingsPage:
        """Return current-user calendar meetings with optional CRM enrichment."""
        if page < 1:
            raise MeetingsTabServiceError("page must be >= 1", 400)
        if limit < 1 or limit > 100:
            raise MeetingsTabServiceError("limit must be between 1 and 100", 400)

        window_start, window_end = self._resolve_window(view, start_at, end_at)
        stmt = self._meeting_rows_stmt().where(
            col(CalendarEvent.start_at) >= window_start,
            col(CalendarEvent.start_at) < window_end,
        )
        if view == MeetingsView.RECORDINGS:
            stmt = stmt.where(col(MeetingRecordingSession.session_id).is_not(None))
        if q:
            like = f"%{q.strip()}%"
            attendee_filter = aliased(CalendarAttendee)
            stmt = stmt.join(
                attendee_filter,
                col(attendee_filter.event_id) == col(CalendarEvent.event_id),
                isouter=True,
            ).where(
                or_(
                    col(CalendarEvent.title).ilike(like),
                    col(CalendarEvent.organiser_email).ilike(like),
                    col(attendee_filter.email).ilike(like),
                    col(attendee_filter.display_name).ilike(like),
                )
            )
        if linked_type == MeetingLinkedRecordType.DEAL:
            stmt = stmt.where(
                or_(
                    col(MeetingRecordLink.record_type) == MeetingRecordLinkType.DEAL.value,
                    col(CalendarEventDealAssociation.association_id).is_not(None),
                )
            )
        elif linked_type:
            stmt = stmt.where(col(MeetingRecordLink.record_type) == linked_type.value)
        if recording_status:
            stmt = stmt.where(col(MeetingRecordingSession.status) == recording_status)

        stmt = stmt.order_by(
            col(CalendarEvent.start_at).desc()
            if view in {MeetingsView.PAST, MeetingsView.RECORDINGS}
            else col(CalendarEvent.start_at).asc(),
            col(CalendarEvent.event_id),
        )

        result = await self.session.execute(stmt)
        rows = result.all()
        domains = await self._load_org_domains()
        items = [self._format_row(row, domains) for row in self._dedupe_rows(rows)]
        if bot_status:
            items = [item for item in items if item.bot.status == bot_status]

        total_count = len(items)
        offset = (page - 1) * limit
        return MeetingsPage(
            items=items[offset : offset + limit],
            total_count=total_count,
            page=page,
            limit=limit,
        )

    async def get_meeting_detail(self, event_id: UUID) -> MeetingDetail:
        """Return drawer-ready detail for one user-owned calendar event."""
        row = await self._get_meeting_row(event_id)
        domains = await self._load_org_domains()
        item = self._format_row(row, domains)
        event, link, assoc, deal, lead, contact, _pref, _recording, _meeting = row
        return MeetingDetail(
            **item.model_dump(),
            description=event.description,
            organiser_name=resolve_attendee_display_name(event.organiser_name, event.organiser_email),
            organiser_email=event.organiser_email,
            linked_record_detail=self._linked_record_detail(link, assoc, deal, lead, contact),
            suggested_actions=self._suggested_actions(item.linked_record),
        )

    async def create_meeting(self, request: CreateMeetingRequest) -> CreateMeetingResponse:
        """Create a provider calendar invite, then apply optional V1 link settings."""
        attendees = await self._attendees_for_create(request)
        invite = CalendarInviteCreateRequest(
            organisation_id=self.organisation_id,
            requested_by_user_id=self.user_id,
            title=request.title,
            body=request.body,
            location=request.location,
            start_at=request.start_at,
            end_at=request.end_at,
            timezone=request.timezone,
            attendees=attendees,
            deal_id=request.link.record_id
            if request.link and request.link.record_type == MeetingLinkedRecordType.DEAL
            else None,
            sender_email=request.sender_email,
            idempotency_key=request.idempotency_key,
        )
        result = await CalendarInviteService(
            self.session,
            organisation_id=self.organisation_id,
            requested_by_user_id=self.user_id,
        ).create_invite(invite)
        if result.event_id is None:
            raise MeetingsTabServiceError("Calendar provider did not return a synced event", 502)

        if request.link:
            await self.set_link(result.event_id, request.link.record_type, request.link.record_id)
        if request.bot_preference:
            await self.set_bot_preference(result.event_id, request.bot_preference)

        return CreateMeetingResponse(
            event_id=result.event_id,
            provider_event_id=result.provider_event_id,
            conference_link=result.conference_link,
            meeting=await self.get_meeting_detail(result.event_id),
            bot_dispatch=None,
        )

    async def set_link(
        self,
        event_id: UUID,
        record_type: MeetingLinkedRecordType,
        record_id: UUID,
    ) -> MeetingDetail:
        """Set the active V1 CRM link for one user-owned meeting."""
        await self._ensure_user_calendar_event(event_id)
        await self._load_record_or_404(record_type, record_id)

        existing_stmt = select(MeetingRecordLink).where(
            col(MeetingRecordLink.organisation_id) == self.organisation_id,
            col(MeetingRecordLink.calendar_event_id) == event_id,
            col(MeetingRecordLink.is_active) == True,  # noqa: E712
        )
        existing = (await self.session.execute(existing_stmt)).scalars().all()
        for link in existing:
            link.is_active = False

        link = MeetingRecordLink(
            organisation_id=self.organisation_id,
            calendar_event_id=event_id,
            record_type=MeetingRecordLinkType(record_type.value),
            record_id=record_id,
            linked_by_user_id=self.user_id,
            is_active=True,
        )
        self.session.add(link)

        keep_deal_id = record_id if record_type == MeetingLinkedRecordType.DEAL else None
        await self._remove_stale_deal_associations(event_id, keep_deal_id=keep_deal_id)
        if record_type == MeetingLinkedRecordType.DEAL:
            await self._mirror_deal_link(event_id, record_id)

        await self.session.flush()
        return await self.get_meeting_detail(event_id)

    async def set_bot_preference(
        self,
        event_id: UUID,
        preference: MeetingBotPreferenceValue,
    ) -> MeetingDetail:
        """Store the current user's per-meeting bot override."""
        await self._ensure_user_calendar_event(event_id)
        stmt = select(MeetingBotPreferenceOverride).where(
            col(MeetingBotPreferenceOverride.organisation_id) == self.organisation_id,
            col(MeetingBotPreferenceOverride.calendar_event_id) == event_id,
            col(MeetingBotPreferenceOverride.user_id) == self.user_id,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing:
            existing.preference = MeetingBotPreference(preference.value)
        else:
            self.session.add(
                MeetingBotPreferenceOverride(
                    organisation_id=self.organisation_id,
                    calendar_event_id=event_id,
                    user_id=self.user_id,
                    preference=MeetingBotPreference(preference.value),
                )
            )
        await self.session.flush()
        return await self.get_meeting_detail(event_id)

    async def invite_bot(self, event_id: UUID) -> dict[str, Any]:
        """Manually send the Recall bot after proving current-user event ownership."""
        await self._ensure_user_calendar_event(event_id)
        await self.session.rollback()
        return await MeetingRecordingDispatchService(self.organisation_id).dispatch_event(event_id)

    async def auto_invite_if_eligible(self, event_id: UUID) -> dict[str, Any] | None:
        """Dispatch the bot after commit when effective policy says auto-invite."""
        row = await self._get_meeting_row(event_id)
        domains = await self._load_org_domains()
        item = self._format_row(row, domains)
        if item.bot.effective_preference != MeetingBotPreferenceValue.AUTO_INVITE:
            return None
        if item.bot.status in {"sent", "already_sent", "recording_live"}:
            return None
        return await self.invite_bot(event_id)

    async def get_recording(self, event_id: UUID) -> MeetingRecordingResponse:
        """Return Recall-bot recording playback and transcript metadata."""
        row = await self._get_meeting_row(event_id)
        event, link, assoc, deal, lead, contact, _pref, recording, _meeting = row
        if recording is None:
            raise MeetingsTabServiceError("Recording is not ready", 409, code="recording_not_ready")

        linked_record = self._linked_record(link, assoc, deal, lead, contact)
        deal_context_assoc = (
            assoc
            if linked_record and linked_record.record_type == MeetingLinkedRecordType.DEAL
            else None
        )
        provider_payload = dict(recording.provider_payload or {})
        bot_snapshot = provider_payload.get("bot_snapshot")
        provider_session_id = recording.provider_session_id
        response_payload = {
            "event_id": event.event_id,
            "recording_status": self._enum_value(recording.status) if recording.status else None,
            "transcript_status": self._enum_value(recording.transcript_status)
            if recording.transcript_status
            else None,
            "provider_session_id": provider_session_id,
            "transcript_artifact": provider_payload.get("transcript_artifact"),
            "summary": deal_context_assoc.meeting_summary_data if deal_context_assoc else None,
            "action_items": self._action_items_from_summary(
                deal_context_assoc.meeting_followup_data if deal_context_assoc else None
            ),
        }

        await self.session.rollback()

        if provider_session_id:
            api_key = await recall_config.get_api_key()
            if api_key:
                provider = RecallMeetingRecordingProvider(
                    api_key=api_key,
                    region=recall_config.RECALL_REGION,
                    timeout=recall_config.RECALL_TIMEOUT_SECONDS,
                )
                bot_snapshot = await provider.fetch_session(provider_session_id)
            else:
                logger.warning("Recall API key missing while resolving playback URL")

        playback_artifact = (
            RecallMeetingRecordingProvider.extract_video_mixed_download(bot_snapshot)
            if isinstance(bot_snapshot, dict)
            else None
        )
        if not playback_artifact:
            raise MeetingsTabServiceError("Recording is not ready", 409, code="recording_not_ready")

        return MeetingRecordingResponse(
            event_id=response_payload["event_id"],
            recording_status=response_payload["recording_status"],
            transcript_status=response_payload["transcript_status"],
            playback_url=playback_artifact.get("download_url"),
            provider_session_id=response_payload["provider_session_id"],
            transcript_artifact=response_payload["transcript_artifact"],
            summary=response_payload["summary"],
            action_items=response_payload["action_items"],
        )

    def _meeting_rows_stmt(self):
        event_c = cast(Any, CalendarEvent).__table__.c
        state_c = cast(Any, CalendarEventUserState).__table__.c
        integration_c = cast(Any, KnowledgeIntegration).__table__.c
        link_c = cast(Any, MeetingRecordLink).__table__.c
        assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
        recording_c = cast(Any, MeetingRecordingSession).__table__.c
        meeting_c = cast(Any, Meeting).__table__.c
        pref_c = cast(Any, MeetingBotPreferenceOverride).__table__.c

        return (
            select(
                CalendarEvent,
                MeetingRecordLink,
                CalendarEventDealAssociation,
                CRMDeal,
                CRMLead,
                CRMContact,
                MeetingBotPreferenceOverride,
                MeetingRecordingSession,
                Meeting,
            )
            .options(selectinload(cast(Any, CalendarEvent).attendees))
            .join(CalendarEventUserState, state_c.event_id == event_c.event_id)
            .join(KnowledgeIntegration, integration_c.integration_id == state_c.integration_id)
            .outerjoin(
                MeetingRecordLink,
                and_(
                    link_c.calendar_event_id == event_c.event_id,
                    link_c.organisation_id == self.organisation_id,
                    link_c.is_active.is_(True),
                ),
            )
            .outerjoin(
                CalendarEventDealAssociation,
                and_(
                    assoc_c.event_id == event_c.event_id,
                    assoc_c.organisation_id == self.organisation_id,
                    assoc_c.status == AssociationStatus.ACTIVE,
                ),
            )
            .outerjoin(
                CRMDeal,
                and_(
                    or_(
                        and_(
                            col(CRMDeal.deal_id) == link_c.record_id,
                            link_c.record_type == MeetingRecordLinkType.DEAL.value,
                        ),
                        col(CRMDeal.deal_id) == assoc_c.deal_id,
                    ),
                    col(CRMDeal.organisation_id) == self.organisation_id,
                    col(CRMDeal.is_deleted) == False,  # noqa: E712
                ),
            )
            .outerjoin(
                CRMLead,
                and_(
                    col(CRMLead.lead_id) == link_c.record_id,
                    link_c.record_type == MeetingRecordLinkType.LEAD.value,
                    col(CRMLead.organisation_id) == self.organisation_id,
                    col(CRMLead.is_deleted) == False,  # noqa: E712
                ),
            )
            .outerjoin(
                CRMContact,
                and_(
                    col(CRMContact.contact_id) == link_c.record_id,
                    link_c.record_type == MeetingRecordLinkType.CONTACT.value,
                    col(CRMContact.organisation_id) == self.organisation_id,
                    col(CRMContact.is_deleted) == False,  # noqa: E712
                ),
            )
            .outerjoin(
                MeetingBotPreferenceOverride,
                and_(
                    pref_c.calendar_event_id == event_c.event_id,
                    pref_c.organisation_id == self.organisation_id,
                    pref_c.user_id == self.user_id,
                ),
            )
            .outerjoin(
                MeetingRecordingSession,
                and_(
                    recording_c.calendar_event_id == event_c.event_id,
                    recording_c.organisation_id == self.organisation_id,
                    recording_c.provider == MeetingRecordingProvider.RECALL_AI.value,
                ),
            )
            .outerjoin(
                Meeting,
                and_(
                    meeting_c.calendar_event_id == event_c.event_id,
                    meeting_c.organisation_id == self.organisation_id,
                    meeting_c.is_deleted.is_(False),
                ),
            )
            .where(
                event_c.organisation_id == self.organisation_id,
                state_c.is_deleted.is_(False),
                integration_c.organisation_id == self.organisation_id,
                integration_c.created_by_user_id == self.user_id,
                integration_c.is_active.is_(True),
                integration_c.integration_type.in_(CALENDAR_INTEGRATION_TYPES),
            )
        )

    async def _get_meeting_row(self, event_id: UUID):
        stmt = self._meeting_rows_stmt().where(col(CalendarEvent.event_id) == event_id).limit(1)
        result = await self.session.execute(stmt)
        row = result.first()
        if not row:
            raise MeetingsTabServiceError("Meeting not found", 404)
        return row

    async def _ensure_user_calendar_event(self, event_id: UUID) -> CalendarEvent:
        row = await self._get_meeting_row(event_id)
        return row[0]

    async def _load_record_or_404(
        self,
        record_type: MeetingLinkedRecordType,
        record_id: UUID,
    ) -> CRMDeal | CRMLead | CRMContact:
        if record_type == MeetingLinkedRecordType.DEAL:
            stmt = select(CRMDeal).where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_id) == record_id,
                col(CRMDeal.is_deleted) == False,  # noqa: E712
            )
        elif record_type == MeetingLinkedRecordType.LEAD:
            stmt = select(CRMLead).where(
                col(CRMLead.organisation_id) == self.organisation_id,
                col(CRMLead.lead_id) == record_id,
                col(CRMLead.is_deleted) == False,  # noqa: E712
            )
        else:
            stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.contact_id) == record_id,
                col(CRMContact.is_deleted) == False,  # noqa: E712
            )
        record = (await self.session.execute(stmt)).scalar_one_or_none()
        if record is None:
            raise MeetingsTabServiceError("Linked record not found", 404)
        return record

    async def _remove_stale_deal_associations(
        self,
        event_id: UUID,
        *,
        keep_deal_id: UUID | None,
    ) -> None:
        stmt = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
            col(CalendarEventDealAssociation.event_id) == event_id,
            col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
        )
        associations = (await self.session.execute(stmt)).scalars().all()
        for assoc in associations:
            if keep_deal_id is not None and assoc.deal_id == keep_deal_id:
                continue
            assoc.status = AssociationStatus.REMOVED

    async def _mirror_deal_link(self, event_id: UUID, deal_id: UUID) -> None:
        stmt = select(CalendarEventDealAssociation).where(
            col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
            col(CalendarEventDealAssociation.event_id) == event_id,
            col(CalendarEventDealAssociation.deal_id) == deal_id,
        )
        assoc = (await self.session.execute(stmt)).scalar_one_or_none()
        if assoc:
            assoc.status = AssociationStatus.ACTIVE
            return
        self.session.add(
            CalendarEventDealAssociation(
                organisation_id=self.organisation_id,
                event_id=event_id,
                deal_id=deal_id,
                status=AssociationStatus.ACTIVE,
                match_score=1.0,
            )
        )

    async def _attendees_for_create(self, request: CreateMeetingRequest) -> list[CalendarInviteAttendee]:
        attendees = list(request.attendees)
        seen = {attendee.email.lower() for attendee in attendees}
        if request.link and request.link.record_type == MeetingLinkedRecordType.LEAD:
            for attendee in await self._lead_attendees(request.link.record_id):
                if attendee.email.lower() not in seen:
                    attendees.append(attendee)
                    seen.add(attendee.email.lower())
        elif request.link and request.link.record_type == MeetingLinkedRecordType.CONTACT:
            contact = cast(CRMContact, await self._load_record_or_404(request.link.record_type, request.link.record_id))
            if contact.email and contact.email.lower() not in seen:
                attendees.append(CalendarInviteAttendee(email=contact.email, name=contact.full_name))
        if not attendees:
            raise MeetingsTabServiceError("At least one attendee is required", 400)
        return attendees

    async def _lead_attendees(self, lead_id: UUID) -> list[CalendarInviteAttendee]:
        lead = cast(CRMLead, await self._load_record_or_404(MeetingLinkedRecordType.LEAD, lead_id))
        attendees: list[CalendarInviteAttendee] = []
        if lead.email:
            attendees.append(CalendarInviteAttendee(email=lead.email, name=lead.full_name))
        stmt = select(CRMLeadParticipant).where(
            col(CRMLeadParticipant.organisation_id) == self.organisation_id,
            col(CRMLeadParticipant.lead_id) == lead_id,
            col(CRMLeadParticipant.is_deleted) == False,  # noqa: E712
            col(CRMLeadParticipant.email).is_not(None),
        )
        participants = (await self.session.execute(stmt)).scalars().all()
        for participant in participants:
            if participant.email:
                attendees.append(CalendarInviteAttendee(email=participant.email, name=participant.name))
        return attendees

    async def _load_org_domains(self) -> set[str]:
        stmt = select(Organisation.domain).where(col(Organisation.organisation_id) == self.organisation_id)
        domains = (await self.master_session.execute(stmt)).scalar_one_or_none() or []
        return {str(domain).lower().removeprefix("www.") for domain in domains if domain}

    def _resolve_window(
        self,
        view: MeetingsView,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> tuple[datetime, datetime]:
        if start_at and end_at:
            return self._as_utc(start_at), self._as_utc(end_at)
        now = datetime.now(UTC)
        try:
            tz = ZoneInfo(self.user_timezone)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        local_today = now.astimezone(tz).date()
        today_start = datetime.combine(local_today, time.min, tzinfo=tz).astimezone(UTC)
        today_end = today_start + timedelta(days=1)
        if view == MeetingsView.TODAY:
            return today_start, today_end
        if view == MeetingsView.UPCOMING:
            return (
                today_end if start_at is None else self._as_utc(start_at),
                self._as_utc(end_at) if end_at else now + timedelta(days=90),
            )
        if view == MeetingsView.PAST:
            return self._as_utc(start_at) if start_at else now - timedelta(days=180), today_start
        if view == MeetingsView.RECORDINGS:
            return (
                self._as_utc(start_at) if start_at else now - timedelta(days=180),
                self._as_utc(end_at) if end_at else now + timedelta(days=90),
            )
        return today_start, today_end

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    @staticmethod
    def _dedupe_rows(rows: list[Any]) -> list[Any]:
        output: list[Any] = []
        seen: set[UUID] = set()
        for row in rows:
            event = row[0]
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            output.append(row)
        return output

    def _format_row(self, row: Any, org_domains: set[str]) -> MeetingListItem:
        event, link, assoc, deal, lead, contact, pref, recording, meeting = row
        attendees = [
            MeetingAttendeeItem(
                email=attendee.email,
                name=resolve_attendee_display_name(attendee.display_name, attendee.email),
                response_status=attendee.response_status,
                is_organiser=attendee.is_organiser,
            )
            for attendee in (event.attendees or [])[:12]
        ]
        linked_record = self._linked_record(link, assoc, deal, lead, contact)
        deal_context_assoc = (
            assoc
            if linked_record and linked_record.record_type == MeetingLinkedRecordType.DEAL
            else None
        )
        is_external = self._is_external_meeting(event, org_domains)
        bot = self._bot_status(pref, recording, linked_record, is_external)
        return MeetingListItem(
            event_id=event.event_id,
            title=event.title,
            start_at=event.start_at,
            end_at=event.end_at,
            duration_minutes=int((event.end_at - event.start_at).total_seconds() / 60)
            if event.start_at and event.end_at
            else None,
            platform=event.online_meeting_provider or event.provider,
            conference_link=event.conference_link,
            status=event.status,
            attendees=attendees,
            attendee_count=event.attendee_count or len(attendees),
            linked_record=linked_record,
            bot=bot,
            recording_status=self._enum_value(recording.status) if recording else None,
            transcript_status=self._enum_value(recording.transcript_status) if recording else None,
            has_recording=recording is not None,
            has_summary=deal_context_assoc is not None
            and deal_context_assoc.meeting_summary_status == GenerationStatus.COMPLETED,
            has_prep=deal_context_assoc is not None
            and deal_context_assoc.meeting_prep_status == GenerationStatus.COMPLETED,
            next_action=self._next_action(deal_context_assoc, meeting, recording),
        )

    def _linked_record(
        self,
        link: MeetingRecordLink | None,
        assoc: CalendarEventDealAssociation | None,
        deal: CRMDeal | None,
        lead: CRMLead | None,
        contact: CRMContact | None,
    ) -> MeetingLinkedRecord | None:
        link_type = str(link.record_type) if link else None
        if link and link_type == MeetingRecordLinkType.DEAL.value and deal:
            return MeetingLinkedRecord(
                record_type=MeetingLinkedRecordType.DEAL,
                record_id=deal.deal_id,
                name=deal.name,
                metadata={"stage": deal.stage_name, "amount": float(deal.amount or 0)},
            )
        if link and link_type == MeetingRecordLinkType.LEAD.value and lead:
            return MeetingLinkedRecord(
                record_type=MeetingLinkedRecordType.LEAD,
                record_id=lead.lead_id,
                name=lead.full_name or lead.company_name or lead.email,
                metadata={"company": lead.company_name, "status": str(lead.status)},
            )
        if link and link_type == MeetingRecordLinkType.CONTACT.value and contact:
            return MeetingLinkedRecord(
                record_type=MeetingLinkedRecordType.CONTACT,
                record_id=contact.contact_id,
                name=contact.full_name or contact.email,
                metadata={"company": contact.company_name, "role": contact.job_title},
            )
        if link is None and assoc is not None and deal:
            return MeetingLinkedRecord(
                record_type=MeetingLinkedRecordType.DEAL,
                record_id=deal.deal_id,
                name=deal.name,
                metadata={"stage": deal.stage_name, "amount": float(deal.amount or 0)},
            )
        return None

    def _linked_record_detail(
        self,
        link: MeetingRecordLink | None,
        assoc: CalendarEventDealAssociation | None,
        deal: CRMDeal | None,
        lead: CRMLead | None,
        contact: CRMContact | None,
    ) -> dict[str, Any] | None:
        compact = self._linked_record(link, assoc, deal, lead, contact)
        if compact is None:
            return None
        return compact.model_dump(mode="json")

    def _bot_status(
        self,
        pref: MeetingBotPreferenceOverride | None,
        recording: MeetingRecordingSession | None,
        linked_record: MeetingLinkedRecord | None,
        is_external: bool,
    ) -> MeetingBotStatus:
        preference = (
            MeetingBotPreferenceValue(self._enum_value(pref.preference))
            if pref and pref.preference
            else None
        )
        effective = preference
        if effective is None and linked_record is not None and is_external:
            effective = MeetingBotPreferenceValue.AUTO_INVITE
        elif effective is None:
            effective = MeetingBotPreferenceValue.ASK_BEFORE

        if preference == MeetingBotPreferenceValue.DO_NOT_INVITE:
            status = "disabled"
        elif recording and self._enum_value(recording.status) in {"IN_CALL", "JOINING"}:
            status = "recording_live"
        elif recording and recording.sent_to_provider_at is not None:
            status = "already_sent"
        elif effective == MeetingBotPreferenceValue.AUTO_INVITE:
            status = "auto_invite_eligible"
        else:
            status = "invite_available"
        return MeetingBotStatus(
            preference=preference,
            effective_preference=effective,
            status=status,
            is_external=is_external,
            provider_session_id=recording.provider_session_id if recording else None,
        )

    @staticmethod
    def _is_external_meeting(event: CalendarEvent, org_domains: set[str]) -> bool:
        if not org_domains:
            return False
        for attendee in event.attendees or []:
            if "@" not in attendee.email:
                continue
            domain = attendee.email.rsplit("@", 1)[-1].lower().removeprefix("www.")
            if domain not in org_domains:
                return True
        return False

    @staticmethod
    def _next_action(
        assoc: CalendarEventDealAssociation | None,
        meeting: Meeting | None,
        recording: MeetingRecordingSession | None,
    ) -> str | None:
        if assoc and assoc.meeting_prep_status != GenerationStatus.COMPLETED:
            return "prepare"
        if recording and meeting and meeting.status == MeetingStatus.READY:
            return "view_recording"
        return None

    @staticmethod
    def _suggested_actions(linked_record: MeetingLinkedRecord | None) -> list[dict[str, Any]]:
        if linked_record is None:
            return []
        return [
            {
                "label": "Create follow-up",
                "record_type": linked_record.record_type,
                "record_id": str(linked_record.record_id),
            }
        ]

    @staticmethod
    def _action_items_from_summary(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            raw_items = value.get("action_items") or value.get("actions") or []
            return [item for item in raw_items if isinstance(item, dict)]
        return []

    @staticmethod
    def _enum_value(value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value)
