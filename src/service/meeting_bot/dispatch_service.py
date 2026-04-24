"""Meeting recording dispatch orchestration.

This service is called from the deal matcher follow-up task and from the hourly
fallback scan. It owns the "should we send this event?" checks before making the
external provider call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.calendar import (
    AssociationStatus,
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
)
from src.infra.config.configs import recall_config
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.meeting_recording.providers import RecallMeetingRecordingProvider
from src.service.meeting_bot.session_service import MeetingRecordingSessionService

logger = get_logger(__name__)


class MeetingRecordingDispatchService:
    """Sends one org-scoped calendar event to the configured recording provider."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id
        self._session_service = MeetingRecordingSessionService(organisation_id)

    async def dispatch_event(self, calendar_event_id: UUID) -> dict[str, str]:
        """Ensure the event has a pending session and send it to Recall once."""
        event = await self._get_calendar_event(calendar_event_id)
        if event is None:
            return {
                "status": "skipped",
                "reason": "event_not_found",
                "calendar_event_id": str(calendar_event_id),
            }

        meeting_url = getattr(event, "conference_link", None)
        if not meeting_url:
            return {
                "status": "skipped",
                "reason": "missing_meeting_url",
                "calendar_event_id": str(calendar_event_id),
            }

        if (getattr(event, "status", None) or "").lower() == "cancelled":
            return {
                "status": "skipped",
                "reason": "cancelled",
                "calendar_event_id": str(calendar_event_id),
            }

        join_at = event.start_at
        if join_at.tzinfo is None:
            join_at = join_at.replace(tzinfo=UTC)

        if join_at <= datetime.now(UTC):
            return {
                "status": "skipped",
                "reason": "meeting_already_started",
                "calendar_event_id": str(calendar_event_id),
            }

        # Persist the intent first so retries/fallback scans can reason about a
        # durable session row instead of trying to recreate provider state.
        session_record = await self._session_service.get_or_create_pending_session(
            calendar_event_id=calendar_event_id,
            meeting_url=meeting_url,
            join_at=join_at,
        )
        if session_record.sent_to_provider_at is not None:
            return {
                "status": "already_sent",
                "calendar_event_id": str(calendar_event_id),
            }

        # Mark queued before the provider call so operators can distinguish
        # "not considered yet" from "send is in flight".
        await self._session_service.mark_send_queued(session_record.session_id)

        provider = await self._build_provider()
        bot_payload = await provider.create_session(
            meeting_url=meeting_url,
            join_at=join_at,
            bot_name=recall_config.RECALL_BOT_NAME,
            metadata={
                # The webhook worker uses this metadata to recover the tenant
                # boundary without trusting only the provider bot id.
                "organisation_id": str(self.organisation_id),
                "calendar_event_id": str(calendar_event_id),
                "dedupe_key": session_record.dedupe_key,
            },
        )

        provider_session_id = bot_payload.get("id")
        if not provider_session_id:
            raise RuntimeError("Recall create bot response missing id")

        await self._session_service.mark_sent(
            session_record.session_id,
            provider_session_id=provider_session_id,
            provider_status=bot_payload.get("status"),
            provider_payload=bot_payload,
        )

        return {
            "status": "sent",
            "calendar_event_id": str(calendar_event_id),
            "provider_session_id": str(provider_session_id),
        }

    async def dispatch_unsent_upcoming(self) -> dict[str, int]:
        """Fallback scan for upcoming deal-linked meetings that were not yet sent."""
        event_ids = await self._list_unsent_upcoming_event_ids()
        summary = {
            "meetings_found": len(event_ids),
            "recordings_sent": 0,
            "already_sent": 0,
            "skipped": 0,
            "failed": 0,
        }

        for event_id in event_ids:
            try:
                result = await self.dispatch_event(event_id)
            except Exception:
                summary["failed"] += 1
                logger.error(
                    "Meeting recording fallback send failed",
                    extra={
                        "event": "meeting_recording_fallback_send_failed",
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(event_id),
                    },
                    exc_info=True,
                )
                continue

            status = result.get("status")
            if status == "sent":
                summary["recordings_sent"] += 1
            elif status == "already_sent":
                summary["already_sent"] += 1
            else:
                summary["skipped"] += 1

        return summary

    async def _build_provider(self) -> RecallMeetingRecordingProvider:
        """Build the provider adapter with deployment-level config."""
        api_key = await recall_config.get_api_key()
        if not api_key:
            raise RuntimeError("Recall API key is not configured")
        return RecallMeetingRecordingProvider(
            api_key=api_key,
            region=recall_config.RECALL_REGION,
            timeout=recall_config.RECALL_TIMEOUT_SECONDS,
        )

    async def _get_calendar_event(self, calendar_event_id: UUID) -> CalendarEvent | None:
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(CalendarEvent).where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEvent.event_id) == calendar_event_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        return None

    async def _list_unsent_upcoming_event_ids(self) -> list[UUID]:
        """Return distinct upcoming deal-linked events with no successful provider send."""
        now = datetime.now(UTC)

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(col(CalendarEvent.event_id))
                .distinct()
                .join(
                    CalendarEventDealAssociation,
                    col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
                )
                .outerjoin(
                    MeetingRecordingSession,
                    (col(MeetingRecordingSession.organisation_id) == self.organisation_id)
                    & (
                        col(MeetingRecordingSession.calendar_event_id)
                        == col(CalendarEvent.event_id)
                    )
                    & (col(MeetingRecordingSession.provider) == MeetingRecordingProvider.RECALL_AI),
                )
                .where(
                    col(CalendarEvent.organisation_id) == self.organisation_id,
                    col(CalendarEvent.start_at) > now,
                    col(CalendarEvent.conference_link).is_not(None),
                    # Only deal-linked active meetings should generate bots.
                    col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                    col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
                    or_(
                        col(CalendarEvent.status).is_(None),
                        col(CalendarEvent.status) != "cancelled",
                    ),
                    # Either no session exists yet or the existing row never
                    # reached a successful provider send.
                    or_(
                        col(MeetingRecordingSession.calendar_event_id).is_(None),
                        col(MeetingRecordingSession.sent_to_provider_at).is_(None),
                    ),
                )
                .order_by(col(CalendarEvent.start_at), col(CalendarEvent.event_id))
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

        return []
