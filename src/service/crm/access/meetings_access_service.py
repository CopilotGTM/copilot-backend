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
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.meeting_recording.providers import RecallProviderError
from src.service.meeting_bot.dispatch_service import MeetingRecordingDispatchService

logger = get_logger("crm_meetings_access_service")

CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)

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

        items = [
            self._format_item(event, recording_session, deals_by_event.get(event.event_id, []))
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
    ) -> CRMMeetingListItem:
        recording = self._format_recording(session)
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
            can_invite_bot=self._can_invite_bot(event, session),
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
    ) -> bool:
        if (
            session is not None
            and _status_value(session.status) != MeetingRecordingStatus.FAILED.value
        ):
            return False
        if not event.conference_link:
            return False
        return (event.status or "").lower() != "cancelled"

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
        self._dispatch_service = MeetingRecordingDispatchService(organisation_id)

    async def invite_bot(self, event_id: UUID) -> dict[str, str]:
        """Dispatch a recording bot for a previously authorized event id."""
        return await self._dispatch_service.dispatch_event(event_id)

    async def cancel_bot(self, event_id: UUID) -> dict[str, str]:
        """Cancel a scheduled recording bot without removing in-call bots."""
        session = await self._dispatch_service._session_service.get_by_event(event_id)  # noqa: SLF001
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

        if session.provider_session_id:
            provider = await self._dispatch_service._build_provider()  # noqa: SLF001
            try:
                await provider.cancel_session(session.provider_session_id)
            except RecallProviderError as exc:
                if exc.status_code != 404:
                    await self._dispatch_service._session_service.mark_cancelled(  # noqa: SLF001
                        session.session_id,
                        status_message=f"Recall cancel failed: {_provider_error_detail(exc)}",
                        provider_payload={"last_cancel_error": _provider_error_payload(exc)},
                    )
                    raise CRMMeetingsAccessServiceError(
                        "Failed to cancel recording bot with provider.",
                        status_code=502,
                    ) from exc
            except Exception as exc:
                await self._dispatch_service._session_service.mark_cancelled(  # noqa: SLF001
                    session.session_id,
                    status_message=f"Recall cancel failed: {_provider_error_detail(exc)}",
                    provider_payload={"last_cancel_error": _provider_error_payload(exc)},
                )
                raise CRMMeetingsAccessServiceError(
                    "Failed to cancel recording bot with provider.",
                    status_code=502,
                ) from exc

        if session.provider_session_id:
            await self._dispatch_service._session_service.mark_cancelled(  # noqa: SLF001
                session.session_id,
                provider_payload={
                    "last_cancel_error": None,
                    "cancelled_provider_session_id": session.provider_session_id,
                },
            )
        else:
            await self._dispatch_service._session_service.mark_cancelled(  # noqa: SLF001
                session.session_id,
            )
        return {
            "status": "cancelled",
            "calendar_event_id": str(event_id),
        }


def _status_value(value: Any) -> str | None:
    """Normalize enum and string-backed SQLModel status fields."""
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _provider_error_detail(exc: Exception) -> str:
    """Return the safest human-readable provider error detail."""
    if isinstance(exc, RecallProviderError) and exc.detail:
        return exc.detail
    return str(exc)


def _provider_error_payload(exc: Exception) -> dict[str, Any]:
    """Build compact provider failure metadata without raw response payloads."""
    payload: dict[str, Any] = {
        "operation": "cancel",
        "detail": _provider_error_detail(exc),
    }
    if isinstance(exc, RecallProviderError):
        payload["status_code"] = exc.status_code
        payload["code"] = exc.code
    return payload


def _has_retryable_cancel_error(session: MeetingRecordingSession) -> bool:
    """Return true when a previous provider delete failed and can be retried."""
    payload = session.provider_payload or {}
    return bool(payload.get("last_cancel_error") and session.provider_session_id)
