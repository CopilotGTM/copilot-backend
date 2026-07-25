"""SSE service for replayable chat message job updates using Redis Streams."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from src.domain.models.organisation_tables.job_models import (
    ChatMessageJob,
    JobStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.redis_channels import channel_for_chat_job
from src.infra.messaging.redis_streams import AsyncRedisStreams
from src.infra.utils.logger import get_logger

logger = get_logger("chat_message_sse_service")


class ChatMessageSSEService:
    """Stream transient events only for the authenticated owner of a chat job."""

    def __init__(self, organisation_id: UUID, job_id: UUID, user_id: UUID) -> None:
        self.organisation_id = str(organisation_id)
        self.job_id = str(job_id)
        self.user_id = str(user_id)
        self.stream_key = channel_for_chat_job(self.organisation_id, self.job_id)
        self.consumer_group = f"sse_consumers_{self.job_id}"
        self.consumer_name = f"user_{self.user_id}"
        self._streams: AsyncRedisStreams | None = None

    async def close(self) -> None:
        """Release the lazily created Redis stream client."""
        try:
            if self._streams:
                await self._streams.close()
        except Exception:  # pragma: no cover
            logger.debug("Redis streams close failed (chat SSE)", exc_info=True)

    async def stream_events(
        self,
        *,
        timeout_seconds: int | None = 900,
        heartbeat_interval: float = 25.0,
        consume_once: bool = False,
        resume_from_id: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Yield SSE events for a chat job from Redis Stream.

        Args:
            timeout_seconds: Maximum duration to keep stream open
            heartbeat_interval: Seconds between ping events
            consume_once: If True, use consumer groups for one-time delivery
            resume_from_id: Last-Event-ID for reconnection handling (SSE standard)
        """
        job_status, result_data, error_message = await self._owned_job_state()
        if job_status == JobStatus.COMPLETED:
            logger.info(
                "Chat SSE: job already completed org=%s job=%s user=%s",
                self.organisation_id,
                self.job_id,
                self.user_id,
            )
            yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
            return

        if job_status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            message = error_message
            if not message and result_data:
                message = str(result_data.get("error") or result_data.get("message") or "")
            if not message:
                message = (
                    "Chat job failed" if job_status == JobStatus.FAILED else "Chat job cancelled"
                )
            yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "job_id": self.job_id,
                        "message": message,
                        "reconnect": False,
                    }
                ),
            }
            yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
            return

        self._initialize_stream()
        self._log_connection_details(consume_once, resume_from_id)

        # Initial ready event
        yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}

        try:
            if consume_once and not resume_from_id:
                async for event in self._stream_with_consumer_group(
                    timeout_seconds, heartbeat_interval, resume_from_id
                ):
                    yield event
                    if event.get("event") == "complete":
                        return
            else:
                async for event in self._stream_legacy_mode(
                    timeout_seconds, heartbeat_interval, resume_from_id
                ):
                    yield event
                    if event.get("event") == "complete":
                        return

        except Exception as e:
            logger.error("Chat SSE stream error: %s", e, exc_info=True)
            yield {
                "event": "error",
                "data": json.dumps({"message": "Internal stream error", "reconnect": True}),
            }
        finally:
            await self.close()

    def _initialize_stream(self) -> None:
        """Initialize the Redis streams client."""
        if self._streams is None:
            self._streams = AsyncRedisStreams()

    def _log_connection_details(self, consume_once: bool, resume_from_id: str | None) -> None:
        """Log connection details for debugging."""
        logger.info(
            "Chat SSE connect org=%s job=%s stream=%s user=%s consume_once=%s resume_from=%s",
            self.organisation_id,
            self.job_id,
            self.stream_key,
            self.user_id,
            consume_once,
            resume_from_id,
        )

    async def _stream_with_consumer_group(
        self,
        timeout_seconds: int | None,
        heartbeat_interval: float,
        resume_from_id: str | None,
    ) -> AsyncGenerator[dict, None]:
        """Stream events using consumer groups for one-time delivery."""
        assert self._streams is not None, "_initialize_stream() must be called first"
        streams = self._streams
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        last_heartbeat = start_time

        if resume_from_id:
            logger.info(
                "Client reconnecting from event_id=%s (consumer group will handle positioning)",
                resume_from_id,
            )

        # Use consumer group for one-time message delivery
        async for entry_id, frame in streams.read_forever_consumer_group(
            self.stream_key,
            self.consumer_group,
            self.consumer_name,
            auto_ack=True,
            block_ms=int((heartbeat_interval / 2) * 1000),
            count=50,
        ):
            # Check for timeout
            timeout_event = self._check_timeout(loop, start_time, timeout_seconds)
            if timeout_event:
                yield timeout_event
                return

            # Send heartbeat if needed
            heartbeat_event = self._check_heartbeat(loop, last_heartbeat, heartbeat_interval)
            if heartbeat_event:
                last_heartbeat = loop.time()
                yield heartbeat_event

            # Send frame event
            yield {"id": entry_id, "event": "frame", "data": json.dumps(frame)}

            # Check for completion
            if frame.get("type") == "finalize":
                yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
                return

    async def _stream_legacy_mode(
        self,
        timeout_seconds: int | None,
        heartbeat_interval: float,
        resume_from_id: str | None,
    ) -> AsyncGenerator[dict, None]:
        """Stream events in legacy mode with replay support."""
        assert self._streams is not None, "_initialize_stream() must be called first"
        streams = self._streams
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        last_heartbeat = start_time
        next_id = resume_from_id or "0-0"

        logger.info("Using legacy replay mode, starting from: %s", next_id)

        while True:
            # Check for timeout
            timeout_event = self._check_timeout(loop, start_time, timeout_seconds)
            if timeout_event:
                yield timeout_event
                return

            # Send heartbeat if needed
            heartbeat_event = self._check_heartbeat(loop, last_heartbeat, heartbeat_interval)
            if heartbeat_event:
                last_heartbeat = loop.time()
                yield heartbeat_event

            # Read from stream
            block_ms = int((heartbeat_interval / 2) * 1000)
            entries = await streams.xread(self.stream_key, next_id, block_ms=block_ms, count=50)
            if not entries:
                continue

            for entry_id, frame in entries:
                next_id = entry_id
                yield {"id": entry_id, "event": "frame", "data": json.dumps(frame)}
                if frame.get("type") == "finalize":
                    yield {
                        "event": "complete",
                        "data": json.dumps({"job_id": self.job_id}),
                    }
                    return

    def _check_timeout(self, loop, start_time: float, timeout_seconds: int | None) -> dict | None:
        """Check if stream has timed out. Returns timeout event if timed out."""
        if timeout_seconds and (loop.time() - start_time) > timeout_seconds:
            return {
                "event": "timeout",
                "data": json.dumps(
                    {
                        "message": "Stream timeout",
                        "job_id": self.job_id,
                        "reconnect": True,
                    }
                ),
            }
        return None

    def _check_heartbeat(
        self, loop, last_heartbeat: float, heartbeat_interval: float
    ) -> dict | None:
        """Check if heartbeat should be sent. Returns heartbeat event if needed."""
        now = loop.time()
        if (now - last_heartbeat) >= heartbeat_interval:
            return {"event": "ping", "data": json.dumps({"t": int(now)})}
        return None

    async def _owned_job_state(
        self,
    ) -> tuple[JobStatus | None, dict | None, str | None]:
        """Return persisted state only when the authenticated user owns the job."""
        async for session in acquire_org_async_session(UUID(self.organisation_id)):
            job = await session.get(ChatMessageJob, UUID(self.job_id))
            # Missing and unowned jobs share one response so callers cannot probe
            # another user's transient chat activity by guessing job IDs.
            if job is None or str(job.user_id) != self.user_id:
                return None, None, None
            return job.status, job.result_data, job.error_message
        return None, None, None

    async def _owned_job_status(self) -> JobStatus | None:
        """Return the job status only when the authenticated user owns the job."""
        status, _, _ = await self._owned_job_state()
        return status

    async def can_stream(self) -> bool:
        """Return whether this user may consume transient events for the job."""
        return await self._owned_job_status() is not None

    async def _is_job_completed(self) -> bool:
        """Return whether this user's job has already completed."""
        return await self._owned_job_status() == JobStatus.COMPLETED
