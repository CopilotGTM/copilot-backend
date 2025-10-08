from __future__ import annotations

"""SSE service for chat message job updates using Redis Streams.

Streams structured frames for a specific chat job over Server-Sent Events with
consume-once support using Redis consumer groups to prevent duplicate message delivery.
"""
import asyncio
import json
from typing import AsyncGenerator, Optional, Dict
from uuid import UUID

from src.infra.messaging.redis_channels import channel_for_chat_job
from src.infra.messaging.redis_streams import AsyncRedisStreams
from src.infra.utils.logger import get_logger
from src.api.dependencies.organisation_dependencies import (
    acquire_org_async_session,
)
from src.domain.models.organisation_tables.job_models import (
    ChatMessageJob,
    JobStatus,
)

logger = get_logger("chat_message_sse_service")


class ChatMessageSSEService:
    def __init__(self, organisation_id: UUID, job_id: UUID, user_id: UUID) -> None:
        self.organisation_id = str(organisation_id)
        self.job_id = str(job_id)
        self.user_id = str(user_id)
        self.stream_key = channel_for_chat_job(self.organisation_id, self.job_id)
        self.consumer_group = f"sse_consumers_{self.job_id}"
        self.consumer_name = f"user_{self.user_id}"
        self._streams: Optional[AsyncRedisStreams] = None

    async def close(self) -> None:
        try:
            if self._streams:
                await self._streams.close()
        except Exception:  # pragma: no cover
            logger.debug("Redis streams close failed (chat SSE)", exc_info=True)

    async def stream_events(
        self,
        *,
        timeout_seconds: Optional[int] = 900,
        heartbeat_interval: float = 25.0,
        consume_once: bool = True,
        resume_from_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        Yield SSE events for a chat job from Redis Stream.

        Args:
            timeout_seconds: Maximum duration to keep stream open
            heartbeat_interval: Seconds between ping events
            consume_once: If True, use consumer groups for one-time delivery
            resume_from_id: Last-Event-ID for reconnection handling (SSE standard)
        """
        # If job already completed, notify frontend and end immediately
        if await self._is_job_completed():
            logger.info(
                "Chat SSE: job already completed org=%s job=%s user=%s",
                self.organisation_id,
                self.job_id,
                self.user_id,
            )
            yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
            return

        self._initialize_stream()
        self._log_connection_details(consume_once, resume_from_id)

        # Initial ready event
        yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}

        try:
            if consume_once:
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
                "data": json.dumps(
                    {"message": "Internal stream error", "reconnect": True}
                ),
            }
        finally:
            await self.close()

    def _initialize_stream(self) -> None:
        """Initialize the Redis streams client."""
        if self._streams is None:
            self._streams = AsyncRedisStreams()

    def _log_connection_details(
        self, consume_once: bool, resume_from_id: Optional[str]
    ) -> None:
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
        timeout_seconds: Optional[int],
        heartbeat_interval: float,
        resume_from_id: Optional[str],
    ) -> AsyncGenerator[Dict, None]:
        """Stream events using consumer groups for one-time delivery."""
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        last_heartbeat = start_time

        if resume_from_id:
            logger.info(
                "Client reconnecting from event_id=%s (consumer group will handle positioning)",
                resume_from_id,
            )

        # Use consumer group for one-time message delivery
        async for entry_id, frame in self._streams.read_forever_consumer_group(
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
            heartbeat_event = self._check_heartbeat(
                loop, last_heartbeat, heartbeat_interval
            )
            if heartbeat_event:
                last_heartbeat = loop.time()
                yield heartbeat_event

            # Send frame event
            yield {"id": entry_id, "event": "frame", "data": json.dumps(frame)}

            # Check for completion
            if frame.get("type") == "finalize":
                yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
                await self._cleanup_completed_job()
                return

    async def _stream_legacy_mode(
        self,
        timeout_seconds: Optional[int],
        heartbeat_interval: float,
        resume_from_id: Optional[str],
    ) -> AsyncGenerator[Dict, None]:
        """Stream events in legacy mode with replay support."""
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
            heartbeat_event = self._check_heartbeat(
                loop, last_heartbeat, heartbeat_interval
            )
            if heartbeat_event:
                last_heartbeat = loop.time()
                yield heartbeat_event

            # Read from stream
            block_ms = int((heartbeat_interval / 2) * 1000)
            entries = await self._streams.xread(
                self.stream_key, next_id, block_ms=block_ms, count=50
            )
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

    def _check_timeout(
        self, loop, start_time: float, timeout_seconds: Optional[int]
    ) -> Optional[Dict]:
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
    ) -> Optional[Dict]:
        """Check if heartbeat should be sent. Returns heartbeat event if needed."""
        now = loop.time()
        if (now - last_heartbeat) >= heartbeat_interval:
            return {"event": "ping", "data": json.dumps({"t": int(now)})}
        return None

    async def _cleanup_completed_job(self) -> None:
        """Clean up Redis stream after job completion to prevent message accumulation."""
        try:
            if self._streams:
                await self._streams.delete_stream(self.stream_key)
                logger.info("Cleaned up completed job stream: %s", self.stream_key)
        except Exception as e:
            logger.warning("Failed to cleanup stream %s: %s", self.stream_key, e)

    async def _is_job_completed(self) -> bool:
        """Check the job's status from the organisation DB and return True if completed."""
        try:
            # Use async org-scoped session to query job status
            completed = False
            async for session in acquire_org_async_session(UUID(self.organisation_id)):
                job = await session.get(ChatMessageJob, UUID(self.job_id))
                if not job:
                    # If job not found, allow streaming (could be transient)
                    logger.debug(
                        "Chat SSE: job %s not found; proceeding to stream", self.job_id
                    )
                    completed = False
                else:
                    completed = job.status == JobStatus.COMPLETED
                break
            return completed
        except Exception:
            # On any failure to check, default to streaming to avoid false negatives
            logger.debug(
                "Chat SSE: failed to check job completion status", exc_info=True
            )
            return False
