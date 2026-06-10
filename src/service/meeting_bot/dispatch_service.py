"""Meeting recording dispatch orchestration.

This service is called from event-driven send tasks and from the nightly fallback
scan. It owns the "should we send this event?" checks before making the external
provider call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventUserState,
)
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
)
from src.infra.config.configs import recall_config
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.meeting_recording.providers import (
    RecallMeetingRecordingProvider,
    RecallProviderError,
)
from src.service.meeting_bot.session_service import MeetingRecordingSessionService

logger = get_logger(__name__)

ACTIVE_CALENDAR_INTEGRATION_TYPES = (
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.OUTLOOK_CALENDAR,
)
DEFAULT_RECALL_BOT_NAME = "Notetaker"


class MeetingRecordingDispatchService:
    """Send one org-scoped calendar event to the recording provider.

    The service is used by both the direct send task and the nightly fallback scan.
    It intentionally keeps decisioning separate from task orchestration so it can be
    reused safely if matching and scheduling are retried.
    """

    def __init__(self, organisation_id: UUID) -> None:
        """Bind all DB and provider work to a single tenant."""
        self.organisation_id = organisation_id
        self._session_service = MeetingRecordingSessionService(organisation_id)
        self._bot_name: str | None = None

    async def dispatch_event(
        self,
        calendar_event_id: UUID,
        *,
        allow_cancelled_reinvite: bool = False,
    ) -> dict[str, str]:
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

        # Calendar cancellations are actionable even when the provider event lost its
        # active user-state row; the local org/event/session boundary is enough to
        # reconcile a bot that was scheduled before the cancellation arrived.
        if (getattr(event, "status", None) or "").lower() == "cancelled":
            return await self._cancel_existing_recording_for_calendar_cancellation(
                calendar_event_id
            )

        if not await self._has_active_calendar_integration(calendar_event_id):
            return {
                "status": "skipped",
                "reason": "calendar_integration_not_active",
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

        join_at = event.start_at
        if join_at.tzinfo is None:
            # A missing timezone means a naive timestamp; assume UTC so ordering and
            # comparisons stay stable across workers.
            join_at = join_at.replace(tzinfo=UTC)

        if join_at <= datetime.now(UTC):
            existing_session = await self._session_service.get_by_event(calendar_event_id)
            if existing_session and existing_session.sent_to_provider_at is not None:
                existing_provider = await self._build_provider()
                if await self._provider_session_matches(
                    provider=existing_provider,
                    provider_session_id=existing_session.provider_session_id,
                    meeting_url=meeting_url,
                    join_at=join_at,
                    calendar_event_id=calendar_event_id,
                    dedupe_key=getattr(existing_session, "dedupe_key", None),
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
        if self._is_cancelled_recording_status(getattr(session_record, "status", None)):
            if not allow_cancelled_reinvite:
                return {
                    "status": "skipped",
                    "reason": "recording_cancelled",
                    "calendar_event_id": str(calendar_event_id),
                }
            if (session_record.provider_payload or {}).get("last_cancel_error"):
                return {
                    "status": "skipped",
                    "reason": "cancel_cleanup_required",
                    "calendar_event_id": str(calendar_event_id),
                }
            reset_record = await self._session_service.reset_cancelled_for_reinvite(
                session_record.session_id,
                meeting_url=meeting_url,
                join_at=join_at,
            )
            if reset_record is None:
                return {
                    "status": "skipped",
                    "reason": "recording_session_missing",
                    "calendar_event_id": str(calendar_event_id),
                }
            session_record = reset_record

        if not await self._has_active_calendar_integration(calendar_event_id):
            return {
                "status": "skipped",
                "reason": "calendar_integration_not_active",
                "calendar_event_id": str(calendar_event_id),
            }

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
                provider_session_status = await self._provider_session_status(
                    provider=provider,
                    provider_session_id=session_record.provider_session_id,
                    meeting_url=meeting_url,
                    join_at=join_at,
                    calendar_event_id=calendar_event_id,
                    dedupe_key=session_record.dedupe_key,
                )
                if provider_session_status == "matches":
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
                    expected_provider_session_id=session_record.provider_session_id,
                    expected_meeting_url=session_record.meeting_url,
                    expected_join_at=session_record.join_at,
                    expected_sent_to_provider_at=session_record.sent_to_provider_at,
                    skip_cancel=provider_session_status == "missing",
                )
                if session_record is None:
                    return {
                        "status": "already_sent",
                        "calendar_event_id": str(calendar_event_id),
                    }
                attempt_adopt_before_create = True

        # Mark queued before the provider call so operators can distinguish
        # "not considered yet" from "send is in flight".
        queued_record = await self._session_service.mark_send_queued(session_record.session_id)
        if queued_record is None:
            return {
                "status": "skipped",
                "reason": "recording_session_missing",
                "calendar_event_id": str(calendar_event_id),
            }
        queued_status = self._status_value(getattr(queued_record, "status", None))
        if queued_status in {
            MeetingRecordingStatus.SENT.value,
            MeetingRecordingStatus.JOINING.value,
            MeetingRecordingStatus.IN_CALL.value,
            MeetingRecordingStatus.COMPLETED.value,
        }:
            return {
                "status": "already_sent",
                "calendar_event_id": str(calendar_event_id),
            }
        if self._is_cancelled_recording_status(getattr(queued_record, "status", None)):
            return {
                "status": "skipped",
                "reason": "recording_cancelled",
                "calendar_event_id": str(calendar_event_id),
            }
        if (
            queued_status == MeetingRecordingStatus.SEND_QUEUED.value
            and "_send_claimed" in queued_record.__dict__
            and not bool(queued_record.__dict__["_send_claimed"])
        ):
            return {
                "status": "skipped",
                "reason": "send_in_progress",
                "calendar_event_id": str(calendar_event_id),
            }

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

        sent_record = await self._session_service.mark_sent(
            session_record.session_id,
            provider_session_id=provider_session_id,
            provider_status=bot_payload.get("status"),
            provider_payload=bot_payload,
        )
        if sent_record is None:
            await self._cancel_created_provider_session_after_local_cancel(
                provider=provider,
                session_id=session_record.session_id,
                calendar_event_id=calendar_event_id,
                provider_session_id=str(provider_session_id),
            )
            return {
                "status": "skipped",
                "reason": "recording_session_missing",
                "calendar_event_id": str(calendar_event_id),
            }
        if self._is_cancelled_recording_status(getattr(sent_record, "status", None)):
            await self._cancel_created_provider_session_after_local_cancel(
                provider=provider,
                session_id=session_record.session_id,
                calendar_event_id=calendar_event_id,
                provider_session_id=str(provider_session_id),
            )
            return {
                "status": "skipped",
                "reason": "recording_cancelled",
                "calendar_event_id": str(calendar_event_id),
            }

        return {
            "status": dispatch_status,
            "calendar_event_id": str(calendar_event_id),
            "provider_session_id": str(provider_session_id),
        }

    async def _cancel_existing_recording_for_calendar_cancellation(
        self,
        calendar_event_id: UUID,
    ) -> dict[str, str]:
        """Cancel any scheduled provider bot after the source calendar event is cancelled."""
        session_record = await self._session_service.get_by_event(calendar_event_id)
        if session_record is None:
            return {
                "status": "skipped",
                "reason": "recording_session_missing",
                "calendar_event_id": str(calendar_event_id),
            }

        provider_payload = dict(session_record.provider_payload or {})
        provider_session_id = getattr(session_record, "provider_session_id", None)
        if self._is_cancelled_recording_status(getattr(session_record, "status", None)):
            if not provider_payload.get("last_cancel_error"):
                return {
                    "status": "cancelled",
                    "calendar_event_id": str(calendar_event_id),
                }
            if not provider_session_id:
                return {
                    "status": "skipped",
                    "reason": "cancel_cleanup_missing_provider_session",
                    "calendar_event_id": str(calendar_event_id),
                }

        if provider_session_id:
            provider = await self._build_provider()
            try:
                await provider.cancel_session(provider_session_id)
            except RecallProviderError as error:
                if error.status_code == 404:
                    pass
                elif self._is_not_deletable_error(error):
                    try:
                        await provider.leave_call(provider_session_id)
                    except RecallProviderError as leave_error:
                        if leave_error.status_code != 404:
                            await self._session_service.mark_cancelled(
                                session_record.session_id,
                                status_message=f"Recall cancel failed: {leave_error}",
                                provider_payload={
                                    "last_cancel_error": {
                                        "operation": "calendar_cancel_leave_call",
                                        "status_code": leave_error.status_code,
                                        "code": leave_error.code,
                                        "detail": leave_error.detail,
                                        "response_body": leave_error.response_body,
                                    }
                                },
                            )
                            raise
                else:
                    await self._session_service.mark_cancelled(
                        session_record.session_id,
                        status_message=f"Recall cancel failed: {error.detail or str(error)}",
                        provider_payload={
                            "last_cancel_error": {
                                "operation": "calendar_cancel",
                                "status_code": error.status_code,
                                "code": error.code,
                                "detail": error.detail,
                                "response_body": error.response_body,
                            }
                        },
                    )
                    raise
            except httpx.HTTPError as error:
                await self._session_service.mark_cancelled(
                    session_record.session_id,
                    status_message=f"Recall cancel failed: {error}",
                    provider_payload={
                        "last_cancel_error": {
                            "operation": "calendar_cancel",
                            "detail": str(error),
                        }
                    },
                )
                raise

        await self._session_service.mark_cancelled(
            session_record.session_id,
            status_message="Recording bot cancelled because the calendar event was cancelled",
            provider_payload={
                "last_cancel_error": None,
                "cancelled_provider_session_id": provider_session_id,
            }
            if provider_session_id
            else None,
        )
        logger.info(
            "Meeting recording provider session cancelled after calendar cancellation",
            extra={
                "event": "meeting_recording_calendar_cancel_succeeded",
                "organisation_id": str(self.organisation_id),
                "calendar_event_id": str(calendar_event_id),
                "provider_session_id": provider_session_id,
            },
        )
        return {
            "status": "cancelled",
            "calendar_event_id": str(calendar_event_id),
        }

    async def dispatch_unsent_upcoming(self) -> dict[str, int]:
        """Scan upcoming active integration meetings and send anything still missing.

        This is intentionally broad and best-effort: if sync or API writes happened
        before the normal task dispatch ran, the scan closes the gap.
        """
        event_ids = await self._list_unsent_upcoming_event_ids()
        upcoming_integration_meetings = await self._count_upcoming_integration_meetings()
        # Deterministic ordering gives stable operator logs and reduces diff noise when
        # rerunning the same scan right after a temporary provider failure.
        summary = {
            "meetings_found": upcoming_integration_meetings,
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
                "meetings_found": upcoming_integration_meetings,
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
        """Compatibility wrapper for older tests and callers."""
        return await self._count_upcoming_integration_meetings()

    async def _count_upcoming_integration_meetings(self) -> int:
        """Count upcoming active integration meetings in the fallback scan window."""
        now = datetime.now(UTC)
        window_end = now + timedelta(days=3)

        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(func.count(col(CalendarEvent.event_id).distinct()))
                .select_from(CalendarEvent)
                .join(
                    CalendarEventUserState,
                    col(CalendarEventUserState.event_id) == col(CalendarEvent.event_id),
                )
                .join(
                    KnowledgeIntegration,
                    col(KnowledgeIntegration.integration_id)
                    == col(CalendarEventUserState.integration_id),
                )
                .where(
                    col(CalendarEvent.organisation_id) == self.organisation_id,
                    col(CalendarEvent.start_at) > now,
                    col(CalendarEvent.start_at) < window_end,
                    col(CalendarEvent.conference_link).is_not(None),
                    col(CalendarEventUserState.is_deleted).is_(False),
                    col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                    col(KnowledgeIntegration.is_active).is_(True),
                    col(KnowledgeIntegration.integration_type).in_(
                        ACTIVE_CALENDAR_INTEGRATION_TYPES
                    ),
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
                bot_name=await self._resolve_bot_name(),
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
        expected_provider_session_id: str | None = None,
        expected_meeting_url: str | None = None,
        expected_join_at: datetime | None = None,
        expected_sent_to_provider_at: datetime | None = None,
    ) -> MeetingRecordingSession | None:
        """Move local state to send-ready after provider cleanup or stale detection."""
        if (
            expected_meeting_url is not None
            and expected_join_at is not None
            and expected_sent_to_provider_at is not None
        ):
            reset_session_record = await self._session_service.reset_for_resend_if_current(
                session_record.session_id,
                expected_provider_session_id=expected_provider_session_id,
                expected_meeting_url=expected_meeting_url,
                expected_join_at=expected_join_at,
                expected_sent_to_provider_at=expected_sent_to_provider_at,
                meeting_url=meeting_url,
                join_at=join_at,
            )
            if (
                reset_session_record is not None
                and "_reset_claimed" in reset_session_record.__dict__
                and not bool(reset_session_record.__dict__["_reset_claimed"])
            ):
                return None
        else:
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
        if not skip_cancel and expected_meeting_url is not None:
            await self._cancel_existing_provider_session_if_possible(
                provider=provider,
                calendar_event_id=calendar_event_id,
                provider_session_id=session_record.provider_session_id,
            )

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
                calendar_event_id=calendar_event_id,
                dedupe_key=session_record.dedupe_key,
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
                bot_name=await self._resolve_bot_name(),
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
                calendar_event_id=calendar_event_id,
                dedupe_key=session_record.dedupe_key,
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

    async def _resolve_bot_name(self) -> str:
        """Return the tenant-branded display name sent to Recall."""
        if self._bot_name is not None:
            return self._bot_name

        organisation_name: str | None = None
        try:
            async for session in acquire_master_async_session():
                stmt = select(col(Organisation.organisation_name)).where(
                    col(Organisation.organisation_id) == self.organisation_id
                )
                result = await session.execute(stmt)
                organisation_name = result.scalar_one_or_none()
                break
        except Exception:
            logger.warning(
                "Meeting recording bot name lookup failed; using fallback",
                extra={
                    "event": "meeting_recording_bot_name_lookup_failed",
                    "organisation_id": str(self.organisation_id),
                },
                exc_info=True,
            )

        self._bot_name = self._format_bot_name(organisation_name)
        return self._bot_name

    @staticmethod
    def _format_bot_name(organisation_name: str | None) -> str:
        """Format the user-visible bot name for meeting attendees."""
        name = organisation_name.strip() if isinstance(organisation_name, str) else ""
        if not name:
            return DEFAULT_RECALL_BOT_NAME
        return f"{name} Notetaker"

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
        calendar_event_id: UUID,
        dedupe_key: str | None,
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
        return self._bot_matches(
            bot_payload,
            meeting_url=meeting_url,
            join_at=join_at,
            calendar_event_id=calendar_event_id,
            dedupe_key=dedupe_key,
            fetched_by_provider_id=True,
        )

    async def _provider_session_status(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        provider_session_id: str | None,
        meeting_url: str,
        join_at: datetime,
        calendar_event_id: UUID,
        dedupe_key: str | None,
    ) -> str:
        """Return whether the stored provider id is matching, missing, or mismatched."""
        if not provider_session_id:
            return "mismatch"
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
                return "missing"
            raise
        if self._bot_matches(
            bot_payload,
            meeting_url=meeting_url,
            join_at=join_at,
            calendar_event_id=calendar_event_id,
            dedupe_key=dedupe_key,
            fetched_by_provider_id=True,
        ):
            return "matches"
        return "mismatch"

    async def _find_matching_provider_session(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        meeting_url: str,
        join_at: datetime,
        calendar_event_id: UUID,
        dedupe_key: str | None,
    ) -> dict[str, Any] | None:
        """Find an upcoming Recall bot for the same meeting URL and start time."""
        window_start = self._normalize_utc(join_at) - timedelta(minutes=15)
        bots = await provider.list_sessions(join_at_after=window_start, meeting_url=meeting_url)
        for bot_payload in bots:
            if self._bot_matches(
                bot_payload,
                meeting_url=meeting_url,
                join_at=join_at,
                calendar_event_id=calendar_event_id,
                dedupe_key=dedupe_key,
                fetched_by_provider_id=False,
            ):
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

    async def _has_active_calendar_integration(self, calendar_event_id: UUID) -> bool:
        """Verify a dispatch task still maps to an active Google/Microsoft calendar."""
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                select(col(CalendarEventUserState.state_id))
                .join(
                    KnowledgeIntegration,
                    col(KnowledgeIntegration.integration_id)
                    == col(CalendarEventUserState.integration_id),
                )
                .where(
                    col(CalendarEventUserState.event_id) == calendar_event_id,
                    col(CalendarEventUserState.is_deleted).is_(False),
                    col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                    col(KnowledgeIntegration.is_active).is_(True),
                    col(KnowledgeIntegration.integration_type).in_(
                        ACTIVE_CALENDAR_INTEGRATION_TYPES
                    ),
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None
        return False

    async def _list_unsent_upcoming_event_ids(self) -> list[UUID]:
        """Return ordered upcoming integration events that are missing send completion."""
        now = datetime.now(UTC)
        window_end = now + timedelta(days=3)

        async for session in acquire_org_async_session(self.organisation_id):
            # Distinct+order_by is required for PostgreSQL compatibility and stable
            # replay ordering. Joining the session table handles "never sent" and
            # "sent_at is NULL" in one pass.
            stmt = (
                select(col(CalendarEvent.event_id), col(CalendarEvent.start_at))
                .distinct()
                .join(
                    CalendarEventUserState,
                    col(CalendarEventUserState.event_id) == col(CalendarEvent.event_id),
                )
                .join(
                    KnowledgeIntegration,
                    col(KnowledgeIntegration.integration_id)
                    == col(CalendarEventUserState.integration_id),
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
                    col(CalendarEvent.start_at) < window_end,
                    col(CalendarEvent.conference_link).is_not(None),
                    col(CalendarEventUserState.is_deleted).is_(False),
                    col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                    col(KnowledgeIntegration.is_active).is_(True),
                    col(KnowledgeIntegration.integration_type).in_(
                        ACTIVE_CALENDAR_INTEGRATION_TYPES
                    ),
                    or_(
                        col(CalendarEvent.status).is_(None),
                        col(CalendarEvent.status) != "cancelled",
                    ),
                    # Either no session exists yet, it was never sent, or the sent
                    # session is stale and needs a resync attempt.
                    or_(
                        col(MeetingRecordingSession.calendar_event_id).is_(None),
                        col(MeetingRecordingSession.status)
                        != MeetingRecordingStatus.CANCELLED.value,
                    ),
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

    async def _cancel_created_provider_session_after_local_cancel(
        self,
        *,
        provider: RecallMeetingRecordingProvider,
        session_id: UUID,
        calendar_event_id: UUID,
        provider_session_id: str,
    ) -> None:
        """Delete a provider bot created after a local cancellation won the race."""
        try:
            await provider.cancel_session(provider_session_id)
        except RecallProviderError as exc:
            if exc.status_code != 404:
                await self._session_service.mark_cancelled(
                    session_id,
                    status_message=f"Recall cancel after local cancellation failed: {exc}",
                    provider_payload={
                        "last_cancel_error": {
                            "operation": "cancel_after_local_cancel",
                            "detail": str(exc),
                        },
                        "orphaned_provider_session_id": provider_session_id,
                    },
                )
                raise
        except Exception as exc:
            await self._session_service.mark_cancelled(
                session_id,
                status_message=f"Recall cancel after local cancellation failed: {exc}",
                provider_payload={
                    "last_cancel_error": {
                        "operation": "cancel_after_local_cancel",
                        "detail": str(exc),
                    },
                    "orphaned_provider_session_id": provider_session_id,
                },
            )
            raise
        await self._session_service.mark_cancelled(
            session_id,
            provider_payload={
                "last_cancel_error": None,
                "cancelled_provider_session_id": provider_session_id,
            },
        )
        logger.info(
            "Meeting recording provider session cancelled after local cancellation",
            extra={
                "event": "meeting_recording_cancelled_after_local_cancel",
                "organisation_id": str(self.organisation_id),
                "calendar_event_id": str(calendar_event_id),
                "provider_session_id": provider_session_id,
            },
        )

    @staticmethod
    def _is_cancelled_recording_status(value: Any) -> bool:
        """Return true for enum or string-backed cancelled session values."""
        if hasattr(value, "value"):
            value = value.value
        return str(value) == MeetingRecordingStatus.CANCELLED.value

    @staticmethod
    def _status_value(value: Any) -> str | None:
        """Normalize enum and string-backed status fields for branch checks."""
        if value is None:
            return None
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    def _bot_matches(
        self,
        bot_payload: dict[str, Any],
        *,
        meeting_url: str,
        join_at: datetime,
        calendar_event_id: UUID,
        dedupe_key: str | None,
        fetched_by_provider_id: bool,
    ) -> bool:
        """Compare Recall bot identity without relying on Teams URL formatting."""
        bot_join_at = self._parse_provider_datetime(bot_payload.get("join_at"))
        if bot_join_at is None:
            return False
        if self._normalize_utc(bot_join_at).replace(microsecond=0) != self._normalize_utc(
            join_at
        ).replace(microsecond=0):
            return False

        metadata = bot_payload.get("metadata")
        if isinstance(metadata, dict) and metadata:
            return (
                metadata.get("organisation_id") == str(self.organisation_id)
                and metadata.get("calendar_event_id") == str(calendar_event_id)
                and (dedupe_key is None or metadata.get("dedupe_key") == dedupe_key)
            )

        bot_url = bot_payload.get("meeting_url")
        if isinstance(bot_url, str):
            return bot_url == meeting_url
        return fetched_by_provider_id

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
        detail = error.detail or ""
        return error.code == "update_bot_failed" or (
            error.status_code == 400
            and bool(detail)
            and ("not enough time" in detail.lower() or "non-dispatched bots" in detail.lower())
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
