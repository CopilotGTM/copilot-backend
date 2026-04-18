from __future__ import annotations

import inspect
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
from src.domain.models.organisation_tables.job_models import ChatMessageJob, JobStatus
from src.domain.models.user_models import RealtimeControlMode
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

VOICE_REALTIME_LOCK_TTL_SECONDS = 120
VOICE_REALTIME_LOCK_HEARTBEAT_SECONDS = 30


def voice_realtime_lock_key(call_id: str) -> str:
    return f"voice_realtime:call:{call_id}"


class VoiceRealtimeStateService:
    def __init__(
        self,
        *,
        organisation_id: UUID,
        user_id: UUID,
        chat_session_id: str,
        session_id: str,
        call_id: str,
        job_id: UUID | None = None,
    ):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.chat_session_id = chat_session_id
        self.session_id = session_id
        self.call_id = call_id
        self.job_id = job_id

    def refresh_lock(self) -> None:
        redis_client = get_redis_client()
        redis_client.set(
            voice_realtime_lock_key(self.call_id),
            self.chat_session_id,
            ex=VOICE_REALTIME_LOCK_TTL_SECONDS,
        )

    def release_lock(self) -> None:
        redis_client = get_redis_client()
        lock_key = voice_realtime_lock_key(self.call_id)
        if redis_client.get(lock_key) == self.chat_session_id:
            redis_client.delete(lock_key)

    async def update_call_state(self, *, status: str, error: str | None = None) -> None:
        async for db in acquire_org_async_session(self.organisation_id):
            result = await db.execute(
                select(ChatSession).where(
                    col(ChatSession.session_id) == UUID(self.chat_session_id),
                    col(ChatSession.user_id) == self.user_id,
                    col(ChatSession.organisation_id) == self.organisation_id,
                    col(ChatSession.chat_type) == ChatType.VOICE_CHAT,
                    col(ChatSession.is_deleted).is_(False),
                )
            )
            chat_session = result.scalars().one_or_none()
            if chat_session is None:
                logger.warning(
                    "Voice realtime chat session disappeared before state update: %s",
                    self.chat_session_id,
                )
                return

            properties = dict(chat_session.properties or {})
            properties.update(
                {
                    "realtime_session_id": self.session_id,
                    "realtime_call_id": self.call_id,
                    "realtime_control_mode": RealtimeControlMode.BACKEND_SIDEBAND.value,
                    "voice_sideband_status": status,
                }
            )
            if status == "queued":
                properties.setdefault("voice_sideband_queued_at", _utcnow_iso())
            if status in {"connecting", "active"} and not properties.get(
                "voice_sideband_started_at"
            ):
                properties["voice_sideband_started_at"] = _utcnow_iso()
            if status in {"completed", "failed"}:
                properties["voice_sideband_ended_at"] = _utcnow_iso()
            if error:
                properties["voice_sideband_error"] = error
            elif status != "failed":
                properties.pop("voice_sideband_error", None)

            chat_session.properties = properties
            db.add(chat_session)
            await self._update_job_state(
                db,
                status=status,
                error_message=error,
            )
            await db.commit()
            return

    async def _update_job_state(
        self,
        db,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None:
        if self.job_id is None:
            return

        job = db.get(ChatMessageJob, self.job_id)
        if inspect.isawaitable(job):
            job = await job
        if job is None:
            return

        job.status = _job_status_for_voice_state(status)
        metadata = dict(job.job_metadata or {})
        metadata.update(
            {
                "chat_session_id": self.chat_session_id,
                "realtime_session_id": self.session_id,
                "realtime_call_id": self.call_id,
                "voice_sideband_status": status,
            }
        )
        job.job_metadata = metadata
        job.error_message = error_message if status == "failed" else None
        db.add(job)


def _utcnow_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _job_status_for_voice_state(status: str) -> JobStatus:
    if status == "completed":
        return JobStatus.COMPLETED
    if status == "failed":
        return JobStatus.FAILED
    if status in {"connecting", "active"}:
        return JobStatus.IN_PROGRESS
    return JobStatus.PENDING


__all__ = [
    "VOICE_REALTIME_LOCK_HEARTBEAT_SECONDS",
    "VoiceRealtimeStateService",
    "voice_realtime_lock_key",
]
