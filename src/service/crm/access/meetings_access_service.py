"""User-owned CRM meeting access and recording controls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.crm_meeting_models import (
    CRMMeetingDealSummary,
    CRMMeetingListItem,
    CRMMeetingRecordingState,
    CRMMeetingsPage,
)
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPES as CALENDAR_INTEGRATION_TYPES,
)
from src.service.meeting_bot.display_status import meeting_recording_display_status
from src.service.meeting_bot.lifecycle_service import (
    MeetingRecordingLifecycleService,
    can_reinvite_prejoin_locally,
)
from src.service.meeting_bot.policy import (
    MEETING_RECORDING_POLICY_NEVER_REASON,
    evaluate_bot_join_policy,
    load_meeting_recording_policy_context,
)
from src.service.meeting_bot.providers import RecallProviderError
from src.service.meeting_bot.recording_window import is_late_join_expired
from src.service.meeting_bot.session_service import MeetingRecordingSessionService
from src.service.orgnisation.organisation_preferences import BOT_JOIN_POLICY_ALL

logger = get_logger("crm_meetings_access_service")

REMOVABLE_RECORDING_STATUSES = {
    MeetingRecordingStatus.PENDING_SEND,
    MeetingRecordingStatus.SEND_QUEUED,
    MeetingRecordingStatus.SENT,
    MeetingRecordingStatus.FAILED,
}
NON_REMOVABLE_RECORDING_STATUSES = {
    MeetingRecordingStatus.JOINING,
    MeetingRecordingStatus.IN_CALL,
    MeetingRecordingStatus.COMPLETED,
}


class CRMMeetingsAccessServiceError(Exception):
    """User-facing meetings API service error."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class CRMMeetingsAccessService:
    """Lists and resolves meetings visible to the current user."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def close(self) -> None:
        """Release the request-scoped session before provider I/O."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("CRMMeetingsAccessService close exception", exc_info=True)

    async def list_meetings(
        self,
        *,
        page: int = 1,
        limit: int = 50,
        start_from: datetime | None = None,
        start_to: datetime | None = None,
        time_scope: str | None = None,
        recording_status: str | None = None,
        linked_deal: bool | None = None,
    ) -> CRMMeetingsPage:
        """Return user-owned synced calendar meetings with safe enrichment only."""
        if page < 1:
            raise CRMMeetingsAccessServiceError("Page must be >= 1", status_code=400)
        limit = max(1, min(limit, 500))

        event_c = cast(Any, CalendarEvent).__table__.c
        session_c = cast(Any, MeetingRecordingSession).__table__.c
        stmt = (
            self._base_visible_events_stmt()
            .add_columns(MeetingRecordingSession)
            .outerjoin(
                MeetingRecordingSession,
                and_(
                    session_c.organisation_id == self.organisation_id,
                    session_c.calendar_event_id == event_c.event_id,
                    session_c.provider == MeetingRecordingProvider.RECALL_AI,
                ),
            )
        )

        now = datetime.now(UTC)
        if time_scope == "upcoming":
            stmt = stmt.where(event_c.start_at >= now)
        elif time_scope == "past":
            stmt = stmt.where(event_c.end_at < now)
        elif time_scope not in (None, "all"):
            raise CRMMeetingsAccessServiceError(
                "time_scope must be one of: upcoming, past, all",
                status_code=400,
            )

        if start_from is not None:
            stmt = stmt.where(event_c.end_at > start_from)
        if start_to is not None:
            stmt = stmt.where(event_c.start_at < start_to)

        if recording_status:
            normalized_status = recording_status.upper()
            if normalized_status == "NOT_REQUESTED":
                stmt = stmt.where(session_c.session_id.is_(None))
            else:
                try:
                    parsed_status = MeetingRecordingStatus(normalized_status)
                except ValueError as err:
                    raise CRMMeetingsAccessServiceError(
                        "Invalid recording_status",
                        status_code=400,
                    ) from err
                stmt = stmt.where(session_c.status == parsed_status)

        if linked_deal is not None:
            exists_clause = self._active_deal_exists_clause(event_c.event_id)
            stmt = stmt.where(exists_clause if linked_deal else ~exists_clause)

        count_stmt = select(func.count()).select_from(
            stmt.with_only_columns(event_c.event_id).order_by(None).subquery()
        )
        total_count = int(await self.org_session.scalar(count_stmt) or 0)

        stmt = (
            stmt.order_by(event_c.start_at.asc(), event_c.event_id.asc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        result = await self.org_session.execute(stmt)
        rows = result.all()
        event_ids = [row[0].event_id for row in rows]
        deals_by_event = await self._list_deals_by_event(event_ids)
        policy_context = await load_meeting_recording_policy_context(self.organisation_id)

        items = [
            self._format_item(
                event,
                recording_session,
                deals_by_event.get(event.event_id, []),
                bot_join_policy=policy_context.bot_join_policy,
                internal_domains=policy_context.internal_domains,
            )
            for event, recording_session in rows
        ]
        total_pages = (total_count + limit - 1) // limit if total_count else 0
        return CRMMeetingsPage(
            items=items,
            page=page,
            limit=limit,
            total_count=total_count,
            total_pages=total_pages,
        )

    async def resolve_visible_event_id(self, event_id: UUID) -> UUID:
        """Prove a raw event id belongs to the current user's active calendar."""
        event_c = cast(Any, CalendarEvent).__table__.c
        stmt = self._base_visible_events_stmt().where(event_c.event_id == event_id).limit(1)
        result = await self.org_session.execute(stmt)
        row = result.first()
        if row is None:
            raise CRMMeetingsAccessServiceError("Meeting not found", status_code=404)
        return cast(UUID, row[0].event_id)

    def _base_visible_events_stmt(self):
        event_c = cast(Any, CalendarEvent).__table__.c
        visible_events = self._visible_event_ids_subquery()
        return (
            select(CalendarEvent)
            .join(visible_events, visible_events.c.event_id == event_c.event_id)
            .where(event_c.organisation_id == self.organisation_id)
        )

    def _visible_event_ids_subquery(self):
        event_c = cast(Any, CalendarEvent).__table__.c
        user_state_c = cast(Any, CalendarEventUserState).__table__.c
        integration_c = cast(Any, KnowledgeIntegration).__table__.c
        return (
            select(event_c.event_id)
            .join(CalendarEventUserState, user_state_c.event_id == event_c.event_id)
            .join(KnowledgeIntegration, integration_c.integration_id == user_state_c.integration_id)
            .where(
                event_c.organisation_id == self.organisation_id,
                user_state_c.is_deleted.is_(False),
                integration_c.organisation_id == self.organisation_id,
                integration_c.created_by_user_id == self.user_id,
                integration_c.is_active.is_(True),
                integration_c.integration_type.in_(CALENDAR_INTEGRATION_TYPES),
            )
            .distinct()
            .subquery()
        )

    def _active_deal_exists_clause(self, event_id_col):
        assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
        return (
            select(assoc_c.association_id)
            .where(
                assoc_c.organisation_id == self.organisation_id,
                assoc_c.event_id == event_id_col,
                assoc_c.status == AssociationStatus.ACTIVE,
            )
            .exists()
        )

    async def _list_deals_by_event(
        self, event_ids: list[UUID]
    ) -> dict[UUID, list[CRMMeetingDealSummary]]:
        if not event_ids:
            return {}
        assoc_c = cast(Any, CalendarEventDealAssociation).__table__.c
        deal_c = cast(Any, CRMDeal).__table__.c
        stmt = (
            select(assoc_c.event_id, CRMDeal)
            .join(CRMDeal, deal_c.deal_id == assoc_c.deal_id)
            .where(
                assoc_c.organisation_id == self.organisation_id,
                assoc_c.event_id.in_(event_ids),
                assoc_c.status == AssociationStatus.ACTIVE,
                deal_c.organisation_id == self.organisation_id,
                or_(deal_c.is_deleted.is_(False), deal_c.is_deleted.is_(None)),
            )
            .order_by(deal_c.name.asc())
        )
        result = await self.org_session.execute(stmt)
        deals_by_event: dict[UUID, list[CRMMeetingDealSummary]] = {}
        for event_id, deal in result.all():
            deals_by_event.setdefault(event_id, []).append(
                CRMMeetingDealSummary(
                    deal_id=deal.deal_id,
                    name=deal.name,
                    company_name=deal.company_name,
                    amount=float(deal.amount) if deal.amount is not None else None,
                    currency=deal.currency,
                    stage_name=deal.stage_name,
                    stage_label=deal.stage_label,
                )
            )
        return deals_by_event

    def _format_item(
        self,
        event: CalendarEvent,
        session: MeetingRecordingSession | None,
        deals: list[CRMMeetingDealSummary],
        *,
        bot_join_policy: str = BOT_JOIN_POLICY_ALL,
        internal_domains: set[str] | None = None,
    ) -> CRMMeetingListItem:
        recording = self._format_recording(session)
        policy_denial_reason = self._bot_policy_denial_reason(
            event,
            bot_join_policy,
            internal_domains or set(),
        )
        return CRMMeetingListItem(
            event_id=event.event_id,
            title=event.title,
            start_at=event.start_at,
            end_at=event.end_at,
            status=event.status,
            location=event.location,
            conference_link=event.conference_link,
            online_meeting_provider=event.online_meeting_provider,
            organiser_name=event.organiser_name,
            organiser_email=event.organiser_email,
            attendee_count=event.attendee_count or 0,
            linked_deals=deals,
            recording=recording,
            can_invite_bot=self._can_invite_bot(
                event,
                session,
                policy_denial_reason=policy_denial_reason,
            ),
            can_remove_bot=self._can_remove_bot(session),
        )

    @staticmethod
    def _format_recording(session: MeetingRecordingSession | None) -> CRMMeetingRecordingState:
        if session is None:
            return CRMMeetingRecordingState()
        return CRMMeetingRecordingState(
            session_id=session.session_id,
            status=session.status.value
            if hasattr(session.status, "value")
            else str(session.status),
            display_status=meeting_recording_display_status(
                recording_status=session.status,
                transcript_status=session.transcript_status,
                status_message=session.status_message,
                sent_to_provider=session.sent_to_provider_at is not None,
            ),
            transcript_status=(
                session.transcript_status.value
                if hasattr(session.transcript_status, "value")
                else str(session.transcript_status)
            ),
            provider=session.provider.value
            if hasattr(session.provider, "value")
            else str(session.provider),
            provider_status=session.provider_status,
            join_at=session.join_at,
            sent_to_provider_at=session.sent_to_provider_at,
        )

    @staticmethod
    def _can_invite_bot(
        event: CalendarEvent,
        session: MeetingRecordingSession | None,
        *,
        policy_denial_reason: str | None = None,
    ) -> bool:
        if not event.conference_link:
            return False
        if policy_denial_reason == MEETING_RECORDING_POLICY_NEVER_REASON:
            return False
        if (event.status or "").lower() == "cancelled":
            return False
        now = datetime.now(UTC)
        end_at = (
            event.end_at.astimezone(UTC)
            if event.end_at.tzinfo
            else event.end_at.replace(tzinfo=UTC)
        )
        if end_at <= now:
            return False
        if session is None:
            return not is_late_join_expired(event.start_at, now)
        return can_reinvite_prejoin_locally(session)

    @staticmethod
    def _bot_policy_denial_reason(
        event: CalendarEvent,
        bot_join_policy: str,
        internal_domains: set[str],
    ) -> str | None:
        result = evaluate_bot_join_policy(
            bot_join_policy=bot_join_policy,
            event=event,
            internal_domains=internal_domains,
        )
        return result.reason if not result.allowed else None

    @staticmethod
    def _can_remove_bot(session: MeetingRecordingSession | None) -> bool:
        if session is None:
            return False
        if _status_value(session.transcript_status) == MeetingTranscriptStatus.READY.value:
            return False
        if _status_value(
            session.status
        ) == MeetingRecordingStatus.CANCELLED.value and _has_retryable_cancel_error(session):
            return True
        return _status_value(session.status) in {
            status.value for status in REMOVABLE_RECORDING_STATUSES
        }


class CRMMeetingRecordingControlService:
    """Provider-facing recording controls after user-event ownership is proven."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id
        self._lifecycle_service = MeetingRecordingLifecycleService(organisation_id)
        self._session_service = MeetingRecordingSessionService(organisation_id)

    async def invite_bot(self, event_id: UUID) -> dict[str, str]:
        """Dispatch a recording bot for a previously authorized event id."""
        return await self._lifecycle_service.schedule_for_event(
            event_id,
            source="crm_manual_invite",
            allow_prejoin_reinvite=True,
            enforce_policy=False,
        )

    async def cancel_bot(self, event_id: UUID) -> dict[str, str]:
        """Cancel a scheduled recording bot without removing in-call bots."""
        session = await self._session_service.get_by_event(event_id)
        if session is None:
            raise CRMMeetingsAccessServiceError("Recording bot not found", status_code=404)
        if _status_value(
            session.status
        ) == MeetingRecordingStatus.CANCELLED.value and not _has_retryable_cancel_error(session):
            return {
                "status": "cancelled",
                "calendar_event_id": str(event_id),
            }
        if (
            _status_value(session.status)
            in {status.value for status in NON_REMOVABLE_RECORDING_STATUSES}
            or _status_value(session.transcript_status) == MeetingTranscriptStatus.READY.value
        ):
            raise CRMMeetingsAccessServiceError(
                "Recording bot cannot be removed after it has joined or completed.",
                status_code=409,
            )

        try:
            return await self._lifecycle_service.cancel_for_event(
                event_id,
                reason="manual_user_cancel",
            )
        except RecallProviderError as exc:
            raise CRMMeetingsAccessServiceError(
                "Failed to cancel recording bot with provider.",
                status_code=502,
            ) from exc


def _status_value(value: Any) -> str | None:
    """Normalize enum and string-backed SQLModel status fields."""
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _has_retryable_cancel_error(session: MeetingRecordingSession) -> bool:
    """Return true when a previous provider delete failed and can be retried."""
    payload = session.provider_payload or {}
    return bool(payload.get("last_cancel_error") and session.provider_session_id)
