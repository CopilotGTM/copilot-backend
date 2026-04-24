"""Persistence helpers for meeting recording sessions.

These helpers are the durable state layer shared by the matcher, the send task,
and the webhook worker. They keep org scoping explicit so provider callbacks
never update a session outside the tenant recovered from webhook metadata.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingProvider,
    MeetingRecordingSession,
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.database.sessions import acquire_org_async_session


class MeetingRecordingSessionService:
    """Owns org-scoped CRUD/status transitions for recording sessions."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

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
        async for session in acquire_org_async_session(self.organisation_id):
            existing = await self._get_by_event(session, calendar_event_id, provider)
            if existing:
                existing.meeting_url = meeting_url
                existing.join_at = join_at
                if existing.sent_to_provider_at is None:
                    # Matcher reruns should refresh pending rows with the latest
                    # join time/link, but once a provider send succeeded we keep
                    # the sent state until an explicit follow-up action changes it.
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
            await session.commit()
            await session.refresh(record)
            return record
        raise RuntimeError("Failed to acquire org session")

    async def mark_send_queued(self, session_id: UUID) -> MeetingRecordingSession | None:
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_session_id(session, session_id)
            if not record:
                return None
            record.status = MeetingRecordingStatus.SEND_QUEUED
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
        async for session in acquire_org_async_session(self.organisation_id):
            record = await self._get_by_session_id(session, session_id)
            if not record:
                return None
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

    async def get_by_provider_session_id(
        self,
        provider_session_id: str,
    ) -> MeetingRecordingSession | None:
        async for session in acquire_org_async_session(self.organisation_id):
            return await self._get_by_provider_session_id(session, provider_session_id)
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
        if after is None:
            after = datetime.now(UTC)
        async for session in acquire_org_async_session(self.organisation_id):
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

    async def _get_by_event(
        self,
        session,
        calendar_event_id: UUID,
        provider: MeetingRecordingProvider,
    ) -> MeetingRecordingSession | None:
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
