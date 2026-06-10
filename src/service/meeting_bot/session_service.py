"""Persistence helpers for meeting recording sessions.

These helpers are the durable state layer shared by the matcher, the send task,
and the webhook worker. They keep org scoping explicit so provider callbacks
never update a session outside the tenant recovered from webhook metadata.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.database.sessions import acquire_org_async_session


class MeetingRecordingSessionService:
    """Owns org-scoped CRUD and status transitions for recording sessions.

    This service is the only place that writes session lifecycle state, so tenant
    isolation and transition safety are centralized here.
    """

    def __init__(self, organisation_id: UUID) -> None:
        """Create a tenant-scoped session service."""
        self.organisation_id = organisation_id
        self.send_queue_stale_after_seconds = 120

    @staticmethod
    def build_dedupe_key(
        organisation_id: UUID,
        calendar_event_id: UUID,
        provider: MeetingRecordingProvider = MeetingRecordingProvider.RECALL_AI,
    ) -> str:
        """Build a stable idempotency key for one org/event/provider tuple."""
        return f"meeting-recording:{organisation_id}:{calendar_event_id}:{provider.value}"

    async def get_or_create_pending_session(
        self,
        *,
        calendar_event_id: UUID,
        meeting_url: str,
        join_at: datetime,
        provider: MeetingRecordingProvider = MeetingRecordingProvider.RECALL_AI,
    ) -> MeetingRecordingSession:
        """Create or refresh a pending session row for one calendar event.

        Existing rows are updated when they are still unsent to preserve retry
        semantics and keep a single stable `session_id` per tenant event/provider.
        Already-sent rows are returned unchanged so dispatch can compare the old
        provider target against the latest calendar event before deciding to resend.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            existing = await self._get_by_event(session, calendar_event_id, provider)
            if existing:
                if self._is_cancelled_status(existing.status):
                    return existing
                if existing.sent_to_provider_at is None:
                    # Matcher reruns should refresh pending rows with the latest
                    # join time/link, but once a provider send succeeded we keep
                    # the sent state until an explicit follow-up action changes it.
                    existing.meeting_url = meeting_url
                    existing.join_at = join_at
                    existing.status = MeetingRecordingStatus.PENDING_SEND
                    existing.status_message = None
                    session.add(existing)
                    await session.commit()
                return existing

            record = MeetingRecordingSession(
                organisation_id=self.organisation_id,
                calendar_event_id=calendar_event_id,
                provider=provider,
                dedupe_key=self.build_dedupe_key(self.organisation_id, calendar_event_id, provider),
                meeting_url=meeting_url,
                join_at=join_at,
                status=MeetingRecordingStatus.PENDING_SEND,
                transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                # Multiple webhook-triggered workers can reach first-session creation
                # together; the unique constraint is the durable winner, so losing
                # workers re-read the row instead of retrying provider work.
                await session.rollback()
                existing = await self._get_by_event(session, calendar_event_id, provider)
                if existing is None:
                    raise
                return existing
            await session.refresh(record)
            return record
        raise RuntimeError("Failed to acquire org session")

    async def mark_send_queued(self, session_id: UUID) -> MeetingRecordingSession | None:
        """Atomically claim a session before the external provider create call.

        Concurrent invite clicks and fallback retries can hit the same event. The
        update below is the send lock: only one caller may move the row into
        `SEND_QUEUED`, while stale queued rows are reclaimable after a crash.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            now = datetime.now(UTC)
            stale_after_seconds = getattr(self, "send_queue_stale_after_seconds", 120)
            stale_before = now - timedelta(seconds=stale_after_seconds)
            stmt = (
                update(MeetingRecordingSession)
                .where(
                    col(MeetingRecordingSession.organisation_id) == self.organisation_id,
                    col(MeetingRecordingSession.session_id) == session_id,
                    or_(
                        col(MeetingRecordingSession.status).in_(
                            [
                                MeetingRecordingStatus.PENDING_SEND,
                                MeetingRecordingStatus.FAILED,
                            ]
                        ),
                        (col(MeetingRecordingSession.status) == MeetingRecordingStatus.SEND_QUEUED)
                        & (col(MeetingRecordingSession.updated_at) < stale_before),
                    ),
                )
                .values(status=MeetingRecordingStatus.SEND_QUEUED, updated_at=now)
                .returning(col(MeetingRecordingSession.session_id))
            )
            result = await session.execute(stmt)
            claimed_session_id = result.scalar_one_or_none()
            await session.commit()
            record = await self._get_by_session_id(session, session_id)
            if record is not None:
                record.__dict__["_send_claimed"] = claimed_session_id is not None
                if claimed_session_id is not None:
                    record.status = MeetingRecordingStatus.SEND_QUEUED
            return record
        return None

    async def reset_cancelled_for_reinvite(
        self,
        session_id: UUID,
        *,
        meeting_url: str,
        join_at: datetime,
    ) -> MeetingRecordingSession | None:
        """Reopen a user-cancelled row only for an explicit manual re-invite."""
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_session_id(session, session_id)
            if not record or not self._is_cancelled_status(record.status):
                return record
            record.meeting_url = meeting_url
            record.join_at = join_at
            record.status = MeetingRecordingStatus.PENDING_SEND
            record.status_message = "Session reset after explicit user re-invite"
            record.sent_to_provider_at = None
            record.provider_session_id = None
            record.provider_status = None
            record.provider_payload = {}
            record.last_webhook_at = None
            record.transcript_ready_at = None
            record.transcript_status = MeetingTranscriptStatus.NOT_REQUESTED
            session.add(record)
            await session.commit()
            return record
        return None

    async def mark_sent(
        self,
        session_id: UUID,
        *,
        provider_session_id: str,
        provider_status: str | None,
        provider_payload: dict,
    ) -> MeetingRecordingSession | None:
        """Mark a session as sent and persist provider response metadata."""
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_session_id(session, session_id)
            if not record:
                return None
            if self._is_cancelled_status(record.status):
                return record
            now = datetime.now(UTC)
            record.status = MeetingRecordingStatus.SENT
            record.transcript_status = MeetingTranscriptStatus.PENDING
            record.provider_session_id = provider_session_id
            record.provider_status = provider_status
            record.provider_payload = provider_payload
            record.sent_to_provider_at = now
            session.add(record)
            await session.commit()
            return record
        return None

    async def update_provider_registration(
        self,
        session_id: UUID,
        *,
        meeting_url: str,
        join_at: datetime,
        provider_session_id: str,
        provider_status: str | None,
        provider_payload: dict[str, Any],
        status_message: str | None = None,
    ) -> MeetingRecordingSession | None:
        """Store a verified provider bot without clearing the org/event identity.

        Used when dispatch updates an existing Recall bot or adopts one found during
        reconciliation. The provider operation has already succeeded, so replacing
        local provider fields here is safe and keeps the row idempotent.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_session_id(session, session_id)
            if not record:
                return None
            if self._is_cancelled_status(record.status):
                return record
            now = datetime.now(UTC)
            record.meeting_url = meeting_url
            record.join_at = join_at
            record.status = MeetingRecordingStatus.SENT
            record.transcript_status = MeetingTranscriptStatus.PENDING
            record.provider_session_id = provider_session_id
            record.provider_status = provider_status
            record.provider_payload = provider_payload
            record.sent_to_provider_at = record.sent_to_provider_at or now
            record.status_message = status_message
            session.add(record)
            await session.commit()
            return record
        return None

    async def mark_provider_failure(
        self,
        session_id: UUID,
        *,
        status_message: str,
        provider_payload: dict[str, Any] | None = None,
    ) -> MeetingRecordingSession | None:
        """Persist provider failure context so retries and operators have evidence."""
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_session_id(session, session_id)
            if not record:
                return None
            if self._is_cancelled_status(record.status):
                return record
            record.status = MeetingRecordingStatus.FAILED
            record.status_message = status_message
            if provider_payload is not None:
                merged_payload = dict(record.provider_payload or {})
                merged_payload["last_provider_error"] = provider_payload
                record.provider_payload = merged_payload
            record.retry_count += 1
            session.add(record)
            await session.commit()
            return record
        return None

    async def reset_for_resend(
        self,
        session_id: UUID,
        *,
        meeting_url: str,
        join_at: datetime,
    ) -> MeetingRecordingSession | None:
        """Clear provider lifecycle fields and move a sent session back to send-ready.

        Called only after dispatch has decided an already-sent provider target is stale.
        The org/event row is preserved for idempotency, while provider-specific state is
        cleared so the next `mark_sent()` records the replacement bot cleanly.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_session_id(session, session_id)
            if not record:
                return None
            if self._is_cancelled_status(record.status):
                return record
            # Refresh only the durable meeting target here. The previous provider id has
            # already been used for best-effort cancellation by the dispatch service.
            record.meeting_url = meeting_url
            record.join_at = join_at
            record.status = MeetingRecordingStatus.PENDING_SEND
            record.status_message = "Session reset for reschedule"
            record.sent_to_provider_at = None
            record.provider_session_id = None
            record.provider_status = None
            record.provider_payload = {}
            record.last_webhook_at = None
            record.transcript_ready_at = None
            record.transcript_status = MeetingTranscriptStatus.NOT_REQUESTED
            session.add(record)
            await session.commit()
            return record
        return None

    async def reset_for_resend_if_current(
        self,
        session_id: UUID,
        *,
        expected_provider_session_id: str | None,
        expected_meeting_url: str,
        expected_join_at: datetime,
        expected_sent_to_provider_at: datetime,
        meeting_url: str,
        join_at: datetime,
    ) -> MeetingRecordingSession | None:
        """Reset only if another worker has not already replaced the provider row."""
        async for session in acquire_org_async_session(self.organisation_id):
            now = datetime.now(UTC)
            provider_condition = (
                col(MeetingRecordingSession.provider_session_id).is_(None)
                if expected_provider_session_id is None
                else col(MeetingRecordingSession.provider_session_id)
                == expected_provider_session_id
            )
            stmt = (
                update(MeetingRecordingSession)
                .where(
                    col(MeetingRecordingSession.organisation_id) == self.organisation_id,
                    col(MeetingRecordingSession.session_id) == session_id,
                    provider_condition,
                    col(MeetingRecordingSession.meeting_url) == expected_meeting_url,
                    col(MeetingRecordingSession.join_at) == expected_join_at,
                    col(MeetingRecordingSession.sent_to_provider_at)
                    == expected_sent_to_provider_at,
                    col(MeetingRecordingSession.status) == MeetingRecordingStatus.SENT,
                )
                .values(
                    meeting_url=meeting_url,
                    join_at=join_at,
                    status=MeetingRecordingStatus.PENDING_SEND,
                    status_message="Session reset for reschedule",
                    sent_to_provider_at=None,
                    provider_session_id=None,
                    provider_status=None,
                    provider_payload={},
                    last_webhook_at=None,
                    transcript_ready_at=None,
                    transcript_status=MeetingTranscriptStatus.NOT_REQUESTED,
                    updated_at=now,
                )
                .returning(col(MeetingRecordingSession.session_id))
            )
            result = await session.execute(stmt)
            reset_session_id = result.scalar_one_or_none()
            await session.commit()
            record = await self._get_by_session_id(session, session_id)
            if record is not None:
                record.__dict__["_reset_claimed"] = reset_session_id is not None
                if reset_session_id is not None:
                    record.meeting_url = meeting_url
                    record.join_at = join_at
                    record.status = MeetingRecordingStatus.PENDING_SEND
                    record.status_message = "Session reset for reschedule"
                    record.sent_to_provider_at = None
                    record.provider_session_id = None
                    record.provider_status = None
                    record.provider_payload = {}
                    record.last_webhook_at = None
                    record.transcript_ready_at = None
                    record.transcript_status = MeetingTranscriptStatus.NOT_REQUESTED
            return record
        return None

    async def get_by_provider_session_id(
        self,
        provider_session_id: str,
    ) -> MeetingRecordingSession | None:
        """Resolve by provider session id for webhook-driven updates."""
        async for session in acquire_org_async_session(self.organisation_id):
            return await self._get_by_provider_session_id(session, provider_session_id)
        return None

    async def get_by_event(
        self,
        calendar_event_id: UUID,
        provider: MeetingRecordingProvider = MeetingRecordingProvider.RECALL_AI,
    ) -> MeetingRecordingSession | None:
        """Resolve an existing session without creating a new send intent.

        Dispatch uses this for already-started meetings: a late manual request
        should report an active existing bot when one exists, but must not create
        a fresh pending session for a call that is already underway.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            return await self._get_by_event(session, calendar_event_id, provider)
        return None

    async def get_by_session_id(self, session_id: UUID) -> MeetingRecordingSession | None:
        """Resolve a session by id after an atomic claim attempt."""
        async for session in acquire_org_async_session(self.organisation_id):
            return await self._get_by_session_id(session, session_id)
        return None

    async def apply_webhook_update(
        self,
        provider_session_id: str,
        *,
        provider_status: str | None,
        provider_payload: dict[str, Any],
        status: MeetingRecordingStatus | None = None,
        transcript_status: MeetingTranscriptStatus | None = None,
        status_message: str | None = None,
        last_webhook_at: datetime | None = None,
        transcript_ready_at: datetime | None = None,
    ) -> MeetingRecordingSession | None:
        """Apply webhook-driven status and payload updates without losing prior state."""
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_provider_session_id(session, provider_session_id)
            if not record:
                return None

            record.provider_payload = provider_payload
            record.provider_status = provider_status
            record.last_webhook_at = last_webhook_at or datetime.now(UTC)

            if status is not None:
                record.status = status
            if transcript_status is not None:
                record.transcript_status = transcript_status
            # Webhooks often update only part of the lifecycle, so callers pass
            # `None` when they intend to preserve the previously known state.
            if status_message is not None:
                record.status_message = status_message
            if transcript_ready_at is not None:
                record.transcript_ready_at = transcript_ready_at

            session.add(record)
            await session.commit()
            return record
        return None

    async def list_unsent_upcoming_sessions(
        self,
        *,
        before: datetime,
        after: datetime | None = None,
    ) -> list[MeetingRecordingSession]:
        """List unsent sessions in a bounded time window, ordered by next join time."""
        if after is None:
            after = datetime.now(UTC)
        async for session in acquire_org_async_session(self.organisation_id):
            # Shared fallback logic uses this ordering for deterministic rescan
            # visibility in logs and for simpler manual replay.
            stmt = (
                select(MeetingRecordingSession)
                .where(
                    col(MeetingRecordingSession.organisation_id) == self.organisation_id,
                    col(MeetingRecordingSession.sent_to_provider_at).is_(None),
                    col(MeetingRecordingSession.join_at) >= after,
                    col(MeetingRecordingSession.join_at) <= before,
                    col(MeetingRecordingSession.status).in_(
                        [
                            MeetingRecordingStatus.PENDING_SEND,
                            MeetingRecordingStatus.SEND_QUEUED,
                            MeetingRecordingStatus.FAILED,
                        ]
                    ),
                )
                .order_by(col(MeetingRecordingSession.join_at))
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
        return []

    async def mark_cancelled(
        self,
        session_id: UUID,
        *,
        status_message: str = "Recording bot cancelled by user",
        provider_payload: dict[str, Any] | None = None,
    ) -> MeetingRecordingSession | None:
        """Mark a recording session as a durable user cancellation."""
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_session_id(session, session_id)
            if not record:
                return None
            record.status = MeetingRecordingStatus.CANCELLED
            record.status_message = status_message
            if provider_payload is not None:
                merged_payload = dict(record.provider_payload or {})
                merged_payload.update(provider_payload)
                record.provider_payload = merged_payload
            session.add(record)
            await session.commit()
            return record
        return None

    @staticmethod
    def _is_cancelled_status(value: Any) -> bool:
        """Handle enum and string-backed status values from different DB drivers."""
        if hasattr(value, "value"):
            value = value.value
        return str(value) == MeetingRecordingStatus.CANCELLED.value

    async def _get_by_event(
        self,
        session,
        calendar_event_id: UUID,
        provider: MeetingRecordingProvider,
    ) -> MeetingRecordingSession | None:
        """Lookup a session by tenant, event, and provider."""
        stmt = select(MeetingRecordingSession).where(
            col(MeetingRecordingSession.organisation_id) == self.organisation_id,
            col(MeetingRecordingSession.calendar_event_id) == calendar_event_id,
            col(MeetingRecordingSession.provider) == provider,
        )
        result = await session.execute(stmt)
        return cast(MeetingRecordingSession | None, result.scalar_one_or_none())

    async def _get_by_session_id(self, session, session_id: UUID) -> MeetingRecordingSession | None:
        stmt = select(MeetingRecordingSession).where(
            col(MeetingRecordingSession.organisation_id) == self.organisation_id,
            col(MeetingRecordingSession.session_id) == session_id,
        )
        result = await session.execute(stmt)
        return cast(MeetingRecordingSession | None, result.scalar_one_or_none())

    async def _get_by_provider_session_id(
        self,
        session,
        provider_session_id: str,
    ) -> MeetingRecordingSession | None:
        stmt = select(MeetingRecordingSession).where(
            col(MeetingRecordingSession.organisation_id) == self.organisation_id,
            col(MeetingRecordingSession.provider_session_id) == provider_session_id,
        )
        result = await session.execute(stmt)
        return cast(MeetingRecordingSession | None, result.scalar_one_or_none())
