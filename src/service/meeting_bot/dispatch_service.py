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
    """Send one org-scoped calendar event to the recording provider.

    The service is used by both the direct send task and the hourly fallback scan.
    It intentionally keeps decisioning separate from task orchestration so it can be
    reused safely if matching and scheduling are retried.
    """

    def __init__(self, organisation_id: UUID) -> None:
        """Bind all DB and provider work to a single tenant."""
        self.organisation_id = organisation_id
        self._session_service = MeetingRecordingSessionService(organisation_id)

    async def dispatch_event(self, calendar_event_id: UUID) -> dict[str, str]:
        """Resolve an event and attempt one provider create call if it is eligible.

        This method is idempotent from the perspective of the caller because the
        session state tracks whether a row was already sent. We never send again when
        the DB already shows a successful provider send.
        """
        event = await self._get_calendar_event(calendar_event_id)
        # Org boundary is enforced here on every lookup. A matching row with a valid
        # external event id is not sufficient by itself, because a stale payload can
        # arrive after tenant reassignment bugs.
        if event is None:
            return {
                "status": "skipped",
                "reason": "event_not_found",
                "calendar_event_id": str(calendar_event_id),
            }

        meeting_url = getattr(event, "conference_link", None)
        # We need a concrete join target before creating a bot; events without links
        # will never produce a successful provider session.
        if not meeting_url:
            return {
                "status": "skipped",
                "reason": "missing_meeting_url",
                "calendar_event_id": str(calendar_event_id),
            }

        # Do not queue cancelled meetings. This avoids duplicate bot churn when
        # calendars are updated between initial matching and fallback polling.
        if (getattr(event, "status", None) or "").lower() == "cancelled":
            return {
                "status": "skipped",
                "reason": "cancelled",
                "calendar_event_id": str(calendar_event_id),
            }

        join_at = event.start_at
        if join_at.tzinfo is None:
            # A missing timezone means a naive timestamp; assume UTC so ordering and
            # comparisons stay stable across workers.
            join_at = join_at.replace(tzinfo=UTC)

        if join_at <= datetime.now(UTC):
            # Past events are intentionally skipped to avoid late-join failures and
            # avoid sending a bot into already-ended calls.
            return {
                "status": "skipped",
                "reason": "meeting_already_started",
                "calendar_event_id": str(calendar_event_id),
            }

        # Persist the intent first so retries/fallback scans can reason about a durable
        # state row instead of trying to recreate provider state from scratch.
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
        # Provider session creation is the only external side effect here. We keep this
        # call behind a short-lived session row so retries can be reconciled without
        # creating duplicate org/event state.
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
        """Scan upcoming deal-linked meetings and send anything still missing.

        This is intentionally broad and best-effort: if matcher created links after
        the normal path ran, the scan closes the gap.
        """
        event_ids = await self._list_unsent_upcoming_event_ids()
        # Deterministic ordering gives stable operator logs and reduces diff noise when
        # rerunning the same scan right after a temporary provider failure.
        summary = {
            "meetings_found": len(event_ids),
            "recordings_sent": 0,
            "already_sent": 0,
            "skipped": 0,
            "failed": 0,
        }
        logger.info(
            "Meeting recording fallback candidates loaded",
            extra={
                "event": "meeting_recording_fallback_candidates_loaded",
                "organisation_id": str(self.organisation_id),
                "meetings_found": len(event_ids),
            },
        )

        for event_id in event_ids:
            try:
                result = await self.dispatch_event(event_id)
            except Exception:
                summary["failed"] += 1
                # Per-event errors are isolated on purpose; one bad event should not
                # block the org-level scan from reaching the rest of the backlog.
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
            logger.info(
                "Meeting recording fallback event processed",
                extra={
                    "event": "meeting_recording_fallback_event_processed",
                    "organisation_id": str(self.organisation_id),
                    "calendar_event_id": str(event_id),
                    "status": status,
                    "reason": result.get("reason"),
                    "provider_session_id": result.get("provider_session_id"),
                },
            )

        logger.info(
            "Meeting recording fallback scan completed",
            extra={
                "event": "meeting_recording_fallback_scan_completed",
                "organisation_id": str(self.organisation_id),
                **summary,
            },
        )
        return summary

    async def _build_provider(self) -> RecallMeetingRecordingProvider:
        """Load provider credentials and return a scoped client.

        The task is allowed to fail fast when key material is missing because retrying
        with the same empty config produces the same failure and only adds noise.
        """
        api_key = await recall_config.get_api_key()
        if not api_key:
            raise RuntimeError("Recall API key is not configured")
        return RecallMeetingRecordingProvider(
            api_key=api_key,
            region=recall_config.RECALL_REGION,
            timeout=recall_config.RECALL_TIMEOUT_SECONDS,
        )

    async def _get_calendar_event(self, calendar_event_id: UUID) -> CalendarEvent | None:
        """Lookup one calendar row for this tenant and event id."""
        async for session in acquire_org_async_session(self.organisation_id):
            # Filter by both tenant and event id so we never trust external IDs as
            # the only tenant boundary.
            stmt = select(CalendarEvent).where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEvent.event_id) == calendar_event_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        return None

    async def _list_unsent_upcoming_event_ids(self) -> list[UUID]:
        """Return ordered upcoming deal-linked events that are missing send completion."""
        now = datetime.now(UTC)

        async for session in acquire_org_async_session(self.organisation_id):
            # Distinct+order_by is required for PostgreSQL compatibility and stable
            # replay ordering. Joining the session table handles "never sent" and
            # "sent_at is NULL" in one pass.
            stmt = (
                select(col(CalendarEvent.event_id), col(CalendarEvent.start_at))
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
