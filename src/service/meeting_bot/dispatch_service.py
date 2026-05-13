"""Meeting recording dispatch orchestration.

This service is called from the deal matcher follow-up task and from the hourly
fallback scan. It owns the "should we send this event?" checks before making the
external provider call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select
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
from src.service.integrations.meeting_recording.providers import (
    RecallMeetingRecordingProvider,
    RecallProviderError,
)
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
        session state tracks whether a row was already sent. A sent row normally
        short-circuits, except when the calendar event has moved and the old provider
        bot needs to be cancelled and recreated for the new join target.
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
            existing_session = await self._session_service.get_by_event(calendar_event_id)
            if existing_session and existing_session.sent_to_provider_at is not None:
                provider = await self._build_provider()
                if await self._provider_session_matches(
                    provider=provider,
                    provider_session_id=existing_session.provider_session_id,
                    meeting_url=meeting_url,
                    join_at=join_at,
                ):
                    return {
                        "status": "already_sent",
                        "calendar_event_id": str(calendar_event_id),
                    }

            # Already-started meetings are intentionally skipped to avoid late-join
            # failures. We check an existing provider bot first so manual "send bot"
            # requests can still report that the bot is already active.
            return {
                "status": "skipped",
                "reason": "meeting_already_started",
                "calendar_event_id": str(calendar_event_id),
            }

        # Persist the intent first so retries/fallback scans can reason about a durable
        # state row instead of trying to recreate provider state from scratch. Sent rows
        # are returned with their old join target intact so drift can be detected below.
        session_record = await self._session_service.get_or_create_pending_session(
            calendar_event_id=calendar_event_id,
            meeting_url=meeting_url,
            join_at=join_at,
        )
        dispatch_status = "sent"
        attempt_adopt_before_create = False
        provider: RecallMeetingRecordingProvider | None = None
        if session_record.sent_to_provider_at is not None:
            # A sent row is only reusable when its stored provider target still matches
            # the calendar event. If the meeting moved, Recall must receive a new bot.
            logger.info(
                "Meeting recording existing session found for event",
                extra={
                    "event": "meeting_recording_existing_session_found",
                    "organisation_id": str(self.organisation_id),
                    "calendar_event_id": str(calendar_event_id),
                    "session_id": str(session_record.session_id),
                    "had_provider_session_id": bool(
                        getattr(session_record, "provider_session_id", None)
                    ),
                    "sent_to_provider_at": session_record.sent_to_provider_at,
                },
            )
            provider = await self._build_provider()
            if self._is_rescheduled(session_record, meeting_url=meeting_url, join_at=join_at):
                dispatch_status = "rescheduled"
                logger.info(
                    "Meeting recording event appears rescheduled; forcing resend",
                    extra={
                        "event": "meeting_recording_rescheduled_event_detected",
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(calendar_event_id),
                        "session_id": str(session_record.session_id),
                        "old_join_at": str(getattr(session_record, "join_at", "")),
                        "new_join_at": str(join_at),
                        "old_meeting_url": str(getattr(session_record, "meeting_url", "")),
                        "new_meeting_url": str(meeting_url),
                    },
                )
                updated = await self._update_provider_session_for_reschedule(
                    provider=provider,
                    session_record=session_record,
                    calendar_event_id=calendar_event_id,
                    meeting_url=meeting_url,
                    join_at=join_at,
                )
                if updated:
                    return {
                        "status": "rescheduled",
                        "calendar_event_id": str(calendar_event_id),
                        "provider_session_id": str(updated.get("id")),
                    }
                session_record = await self._prepare_replacement_send(
                    provider=provider,
                    session_record=session_record,
                    calendar_event_id=calendar_event_id,
                    meeting_url=meeting_url,
                    join_at=join_at,
                )
                attempt_adopt_before_create = True
            else:
                if await self._provider_session_matches(
                    provider=provider,
                    provider_session_id=session_record.provider_session_id,
                    meeting_url=meeting_url,
                    join_at=join_at,
                ):
                    return {
                        "status": "already_sent",
                        "calendar_event_id": str(calendar_event_id),
                    }
                session_record = await self._prepare_replacement_send(
                    provider=provider,
                    session_record=session_record,
                    calendar_event_id=calendar_event_id,
                    meeting_url=meeting_url,
                    join_at=join_at,
                    skip_cancel=True,
                )
                attempt_adopt_before_create = True

        # Mark queued before the provider call so operators can distinguish
        # "not considered yet" from "send is in flight".
        await self._session_service.mark_send_queued(session_record.session_id)

        if provider is None:
            provider = await self._build_provider()
        # Provider session creation is the only external side effect here. We keep this
        # call behind a short-lived session row so retries can be reconciled without
        # creating duplicate org/event state.
        bot_payload = await self._create_or_adopt_provider_session(
            provider=provider,
            session_record=session_record,
            calendar_event_id=calendar_event_id,
            meeting_url=meeting_url,
            join_at=join_at,
            attempt_adopt_before_create=attempt_adopt_before_create,
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
            "status": dispatch_status,
            "calendar_event_id": str(calendar_event_id),
            "provider_session_id": str(provider_session_id),
        }

    async def dispatch_unsent_upcoming(self) -> dict[str, int]:
        """Scan upcoming deal-linked meetings and send anything still missing.

        This is intentionally broad and best-effort: if matcher created links after
        the normal path ran, the scan closes the gap.
        """
        event_ids = await self._list_unsent_upcoming_event_ids()
        upcoming_deal_meetings = await self._count_upcoming_deal_meetings()
        # Deterministic ordering gives stable operator logs and reduces diff noise when
        # rerunning the same scan right after a temporary provider failure.
        summary = {
            "meetings_found": upcoming_deal_meetings,
            "actionable_meetings_found": len(event_ids),
            "recordings_sent": 0,
            "already_sent": 0,
            "rescheduled": 0,
            "skipped": 0,
            "failed": 0,
        }
        logger.info(
            "Meeting recording fallback candidates loaded",
            extra={
                "event": "meeting_recording_fallback_candidates_loaded",
                "organisation_id": str(self.organisation_id),
                "meetings_found": upcoming_deal_meetings,
                "actionable_meetings_found": len(event_ids),
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
            elif status == "rescheduled":
                summary["rescheduled"] += 1
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

    async def _count_upcoming_deal_meetings(self) -> int:
        """Count upcoming active deal-linked meetings, regardless of bot state."""
        now = datetime.now(UTC)

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(func.count(col(CalendarEvent.event_id).distinct()))
                .select_from(CalendarEvent)
                .join(
                    CalendarEventDealAssociation,
                    col(CalendarEventDealAssociation.event_id) == col(CalendarEvent.event_id),
                )
                .where(
                    col(CalendarEvent.organisation_id) == self.organisation_id,
                    col(CalendarEvent.start_at) > now,
                    col(CalendarEvent.conference_link).is_not(None),
                    col(CalendarEventDealAssociation.organisation_id) == self.organisation_id,
                    col(CalendarEventDealAssociation.status) == AssociationStatus.ACTIVE,
                    or_(
                        col(CalendarEvent.status).is_(None),
                        col(CalendarEvent.status) != "cancelled",
                    ),
                )
            )
            result = await session.execute(stmt)
            return int(result.scalar_one() or 0)

        return 0

    def _provider_metadata(
        self,
        *,
        calendar_event_id: UUID,
        dedupe_key: str,
    ) -> dict[str, str]:
        """Build Recall metadata used later to recover tenant and event scope."""
        return {
            "organisation_id": str(self.organisation_id),
            "calendar_event_id": str(calendar_event_id),
            "dedupe_key": dedupe_key,
        }

    async def _update_provider_session_for_reschedule(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        session_record: MeetingRecordingSession,
        calendar_event_id: UUID,
        meeting_url: str,
        join_at: datetime,
    ) -> dict[str, Any] | None:
        """Try Recall's update-first reschedule path before creating replacements."""
        provider_session_id = session_record.provider_session_id
        if not provider_session_id:
            return None
        try:
            bot_payload = await provider.update_session(
                provider_session_id,
                meeting_url=meeting_url,
                join_at=join_at,
                bot_name=recall_config.RECALL_BOT_NAME,
                metadata=self._provider_metadata(
                    calendar_event_id=calendar_event_id,
                    dedupe_key=session_record.dedupe_key,
                ),
            )
        except RecallProviderError as error:
            if self._is_update_fallback_error(error):
                logger.warning(
                    "Meeting recording provider update rejected; falling back to replacement",
                    extra={
                        "event": "meeting_recording_reschedule_update_rejected",
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(calendar_event_id),
                        "provider_session_id": provider_session_id,
                        "status_code": error.status_code,
                        "provider_code": error.code,
                        "provider_detail": error.detail,
                    },
                )
                return None
            await self._record_provider_failure(session_record, "update", error)
            raise

        updated_id = bot_payload.get("id") or provider_session_id
        bot_payload.setdefault("id", updated_id)
        await self._session_service.update_provider_registration(
            session_record.session_id,
            meeting_url=meeting_url,
            join_at=join_at,
            provider_session_id=str(updated_id),
            provider_status=bot_payload.get("status"),
            provider_payload=bot_payload,
            status_message="Provider session updated after calendar reschedule",
        )
        return bot_payload

    async def _prepare_replacement_send(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        session_record: MeetingRecordingSession,
        calendar_event_id: UUID,
        meeting_url: str,
        join_at: datetime,
        skip_cancel: bool = False,
    ) -> MeetingRecordingSession:
        """Move local state to send-ready after provider cleanup or stale detection."""
        if not skip_cancel:
            await self._cancel_existing_provider_session_if_possible(
                provider=provider,
                calendar_event_id=calendar_event_id,
                provider_session_id=session_record.provider_session_id,
            )
        reset_session_record = await self._session_service.reset_for_resend(
            session_record.session_id,
            meeting_url=meeting_url,
            join_at=join_at,
        )
        if not reset_session_record:
            raise RuntimeError("Failed to reset meeting recording session for resend")

        logger.info(
            "Meeting recording session reset and marked for resend",
            extra={
                "event": "meeting_recording_session_reset_for_resend",
                "organisation_id": str(self.organisation_id),
                "calendar_event_id": str(calendar_event_id),
                "session_id": str(session_record.session_id),
            },
        )
        return reset_session_record

    async def _create_or_adopt_provider_session(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        session_record: MeetingRecordingSession,
        calendar_event_id: UUID,
        meeting_url: str,
        join_at: datetime,
        attempt_adopt_before_create: bool = False,
    ) -> dict[str, Any]:
        """Create a Recall bot, adopting an existing matching one when found."""
        if attempt_adopt_before_create:
            matching_bot = await self._find_matching_provider_session(
                provider=provider,
                meeting_url=meeting_url,
                join_at=join_at,
            )
            if matching_bot:
                await self._adopt_provider_session(
                    session_record=session_record,
                    meeting_url=meeting_url,
                    join_at=join_at,
                    bot_payload=matching_bot,
                )
                return matching_bot

        try:
            return await provider.create_session(
                meeting_url=meeting_url,
                join_at=join_at,
                bot_name=recall_config.RECALL_BOT_NAME,
                metadata=self._provider_metadata(
                    calendar_event_id=calendar_event_id,
                    dedupe_key=session_record.dedupe_key,
                ),
            )
        except RecallProviderError as error:
            logger.warning(
                "Meeting recording provider create failed; checking for adoptable bot",
                extra={
                    "event": "meeting_recording_create_failed_reconcile_attempt",
                    "organisation_id": str(self.organisation_id),
                    "calendar_event_id": str(calendar_event_id),
                    "session_id": str(session_record.session_id),
                    "status_code": error.status_code,
                    "provider_code": error.code,
                    "provider_detail": error.detail,
                },
            )
            matching_bot = await self._find_matching_provider_session(
                provider=provider,
                meeting_url=meeting_url,
                join_at=join_at,
            )
            if matching_bot:
                await self._adopt_provider_session(
                    session_record=session_record,
                    meeting_url=meeting_url,
                    join_at=join_at,
                    bot_payload=matching_bot,
                )
                return matching_bot
            await self._record_provider_failure(session_record, "create", error)
            raise

    async def _adopt_provider_session(
        self,
        *,
        session_record: MeetingRecordingSession,
        meeting_url: str,
        join_at: datetime,
        bot_payload: dict[str, Any],
    ) -> None:
        """Attach a locally stale row to an already-scheduled matching Recall bot."""
        provider_session_id = bot_payload.get("id")
        if not provider_session_id:
            raise RuntimeError("Recall matching bot response missing id")
        await self._session_service.update_provider_registration(
            session_record.session_id,
            meeting_url=meeting_url,
            join_at=join_at,
            provider_session_id=str(provider_session_id),
            provider_status=bot_payload.get("status"),
            provider_payload=bot_payload,
            status_message="Provider session adopted during reconciliation",
        )

    async def _provider_session_matches(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        provider_session_id: str | None,
        meeting_url: str,
        join_at: datetime,
    ) -> bool:
        """Verify the local provider id still points at the expected Recall bot."""
        if not provider_session_id:
            return False
        try:
            bot_payload = await provider.fetch_session(provider_session_id)
        except RecallProviderError as error:
            if error.status_code == 404:
                logger.warning(
                    "Meeting recording provider session missing during reconciliation",
                    extra={
                        "event": "meeting_recording_provider_session_missing",
                        "organisation_id": str(self.organisation_id),
                        "provider_session_id": provider_session_id,
                    },
                )
                return False
            raise
        return self._bot_matches(bot_payload, meeting_url=meeting_url, join_at=join_at)

    async def _find_matching_provider_session(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        meeting_url: str,
        join_at: datetime,
    ) -> dict[str, Any] | None:
        """Find an upcoming Recall bot for the same meeting URL and start time."""
        window_start = self._normalize_utc(join_at) - timedelta(minutes=15)
        bots = await provider.list_sessions(join_at_after=window_start, meeting_url=meeting_url)
        for bot_payload in bots:
            if self._bot_matches(bot_payload, meeting_url=meeting_url, join_at=join_at):
                return bot_payload
        return None

    async def _record_provider_failure(
        self,
        session_record: MeetingRecordingSession,
        operation: str,
        error: RecallProviderError,
    ) -> None:
        """Persist the latest provider error without hiding the original exception."""
        await self._session_service.mark_provider_failure(
            session_record.session_id,
            status_message=f"Recall {operation} failed: {error.detail or str(error)}",
            provider_payload={
                "operation": operation,
                "status_code": error.status_code,
                "code": error.code,
                "detail": error.detail,
                "response_body": error.response_body,
            },
        )

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
                    # Either no session exists yet, it was never sent, or the sent
                    # session is stale and needs a resync attempt.
                    or_(
                        col(MeetingRecordingSession.calendar_event_id).is_(None),
                        col(MeetingRecordingSession.sent_to_provider_at).is_(None),
                        col(MeetingRecordingSession.join_at) != col(CalendarEvent.start_at),
                        col(MeetingRecordingSession.meeting_url)
                        != col(CalendarEvent.conference_link),
                    ),
                )
                .order_by(col(CalendarEvent.start_at), col(CalendarEvent.event_id))
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

        return []

    @staticmethod
    def _normalize_utc(value: datetime) -> datetime:
        """Normalize DB and calendar timestamps into comparable UTC form."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _is_rescheduled(
        self,
        session_record: MeetingRecordingSession,
        *,
        meeting_url: str,
        join_at: datetime,
    ) -> bool:
        """Compare incoming event details to the provider target stored on the session.

        Called only for already-sent rows. Unsent rows are allowed to refresh in place,
        while sent rows must preserve their old join target until this drift check runs.
        """
        existing_join_at = getattr(session_record, "join_at", None)
        existing_meeting_url = getattr(session_record, "meeting_url", None)
        if existing_join_at is None or not existing_meeting_url:
            return False
        return (
            self._normalize_utc(existing_join_at) != self._normalize_utc(join_at)
            or existing_meeting_url != meeting_url
        )

    @classmethod
    def _bot_matches(
        cls,
        bot_payload: dict[str, Any],
        *,
        meeting_url: str,
        join_at: datetime,
    ) -> bool:
        """Compare Recall bot identity using URL plus meeting instance time."""
        bot_url = bot_payload.get("meeting_url")
        bot_join_at = cls._parse_provider_datetime(bot_payload.get("join_at"))
        if bot_url != meeting_url or bot_join_at is None:
            return False
        return cls._normalize_utc(bot_join_at).replace(microsecond=0) == cls._normalize_utc(
            join_at
        ).replace(microsecond=0)

    @staticmethod
    def _parse_provider_datetime(value: Any) -> datetime | None:
        """Parse Recall ISO timestamps, including the common trailing-Z form."""
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    @staticmethod
    def _is_update_fallback_error(error: RecallProviderError) -> bool:
        """Identify Recall update failures that require replacement scheduling."""
        return error.code == "update_bot_failed" or (
            error.status_code == 400
            and bool(error.detail)
            and (
                "not enough time" in error.detail.lower()
                or "non-dispatched bots" in error.detail.lower()
            )
        )

    @staticmethod
    def _is_not_deletable_error(error: RecallProviderError) -> bool:
        """Identify provider delete failures where leave_call is the right cleanup."""
        return error.code == "cannot_delete_bot" or error.status_code == 405

    async def _cancel_existing_provider_session_if_possible(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        calendar_event_id: UUID,
        provider_session_id: str | None,
    ) -> None:
        """Best-effort old bot cancel to reduce duplicate Recall bots after reschedule."""
        if not provider_session_id:
            # Missing provider ids can happen on manually repaired rows. The resend can
            # still succeed, so keep the warning operational instead of blocking dispatch.
            logger.warning(
                "Meeting recording reschedule reset has no provider session id to cancel",
                extra={
                    "event": "meeting_recording_reschedule_cancel_skipped",
                    "organisation_id": str(self.organisation_id),
                    "calendar_event_id": str(calendar_event_id),
                },
            )
            return

        try:
            await provider.cancel_session(provider_session_id)
            logger.info(
                "Meeting recording prior provider session cancelled",
                extra={
                    "event": "meeting_recording_reschedule_cancel_succeeded",
                    "organisation_id": str(self.organisation_id),
                    "calendar_event_id": str(calendar_event_id),
                    "provider_session_id": provider_session_id,
                },
            )
        except RecallProviderError as error:
            if error.status_code == 404:
                # A missing bot is equivalent to an already-completed cleanup. Continuing
                # lets the new schedule overwrite stale local provider state.
                logger.warning(
                    "Meeting recording prior provider session already gone; continuing",
                    extra={
                        "event": "meeting_recording_reschedule_cancel_404",
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(calendar_event_id),
                        "provider_session_id": provider_session_id,
                    },
                )
                return
            if self._is_not_deletable_error(error):
                # Recall refuses deletion once a bot has joined or left the cancellable
                # scheduled state. Ask the in-call bot to leave, then continue with the
                # replacement send instead of leaving the updated meeting unscheduled.
                try:
                    await provider.leave_call(provider_session_id)
                except RecallProviderError as leave_error:
                    if leave_error.status_code != 404:
                        raise
                logger.warning(
                    "Meeting recording prior provider session removed via leave-call",
                    extra={
                        "event": "meeting_recording_reschedule_cancel_not_deletable",
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(calendar_event_id),
                        "provider_session_id": provider_session_id,
                        "provider_code": error.code,
                        "provider_detail": error.detail,
                    },
                )
                return
            raise
        except httpx.HTTPError:
            # Network failures are deliberately non-fatal. The provider create below is
            # the user-visible fix, and a later webhook/status check can expose stragglers.
            logger.warning(
                "Meeting recording provider cancel failed with transport error; continuing",
                extra={
                    "event": "meeting_recording_reschedule_cancel_transport_error",
                    "organisation_id": str(self.organisation_id),
                    "calendar_event_id": str(calendar_event_id),
                    "provider_session_id": provider_session_id,
                },
            )
