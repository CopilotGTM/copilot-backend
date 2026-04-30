"""SSE service for deal job updates using Redis Streams.

Works with any DealJobType - the job_type is included in each frame.
Handles job recovery when users reconnect after disconnection.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from src.domain.models.organisation_tables.job_models import DealJobs, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.redis_channels import channel_for_deal_job
from src.infra.messaging.redis_streams import AsyncRedisStreams
from src.infra.utils.logger import get_logger

logger = get_logger("deal_job_sse_service")

FRAME_TYPE_ANSWER_REPLACE = "answer.replace"
FRAME_TYPE_FINALIZE = "finalize"


class DealJobSSEService:
    """
    Generic SSE service for streaming deal job updates.

    Works with any DealJobType - the job_type is included in each frame.
    Supports job recovery when user reconnects (resume from where they left off).
    """

    def __init__(self, organisation_id: UUID, job_id: UUID, user_id: UUID) -> None:
        self.organisation_id = str(organisation_id)
        self.job_id = str(job_id)
        self.user_id = str(user_id)
        self.stream_key = channel_for_deal_job(self.organisation_id, self.job_id)
        self._streams: AsyncRedisStreams | None = None

    async def close(self) -> None:
        """Close the Redis streams connection."""
        try:
            if self._streams:
                await self._streams.close()
        except Exception:
            logger.debug("Redis streams close failed (deal job SSE)", exc_info=True)

    async def stream_events(
        self,
        *,
        resume_from_id: str | None = None,
        timeout_seconds: int = 300,  # 5 min timeout
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[dict, None]:
        """
        Yield SSE events for any deal job from Redis Stream.

        Handles the following edge cases:
        1. Job already completed - returns cached result immediately
        2. Job in progress - streams events from Redis
        3. User reconnects mid-job - resumes from Last-Event-ID when provided

        Args:
            timeout_seconds: Max time before timing out the stream
            heartbeat_interval: Time between heartbeat pings
        """

        # Check job status first (handles reconnection and authorization scenarios)
        job_status, result_data, access_denied = await self._get_job_status()

        if access_denied:
            logger.warning(
                "Deal job SSE access denied org=%s job=%s user=%s",
                self.organisation_id,
                self.job_id,
                self.user_id,
            )
            yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "job_id": self.job_id,
                        "message": "Access denied for this job",
                        "reconnect": False,
                    }
                ),
            }
            return

        if job_status == JobStatus.COMPLETED:
            # Job already done: replay a final frame before completion so reconnecting
            # clients can use the same terminal handling path as live streams.
            logger.info(
                "Deal job SSE: job already completed org=%s job=%s",
                self.organisation_id,
                self.job_id,
            )
            yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}
            if result_data is not None:
                yield {
                    "event": "frame",
                    "data": json.dumps({"type": FRAME_TYPE_ANSWER_REPLACE, "content": result_data}),
                }
            yield {
                "event": "frame",
                "data": json.dumps({"type": FRAME_TYPE_FINALIZE, "reason": "done"}),
            }
            yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
            return

        if job_status == JobStatus.FAILED:
            logger.info(
                "Deal job SSE: job failed org=%s job=%s",
                self.organisation_id,
                self.job_id,
            )
            error_message = "Job failed"
            if result_data:
                error_message = str(
                    result_data.get("error") or result_data.get("message") or error_message
                )
            yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "job_id": self.job_id,
                        "message": error_message,
                        "reconnect": False,
                    }
                ),
            }
            yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
            return

        if job_status == JobStatus.CANCELLED:
            logger.info(
                "Deal job SSE: job cancelled org=%s job=%s",
                self.organisation_id,
                self.job_id,
            )
            yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "job_id": self.job_id,
                        "message": "Job cancelled",
                        "reconnect": False,
                    }
                ),
            }
            yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
            return

        # Job is pending or in progress - stream from Redis
        self._streams = AsyncRedisStreams()

        logger.info(
            "Deal job SSE connect org=%s job=%s stream=%s user=%s resume_from=%s",
            self.organisation_id,
            self.job_id,
            self.stream_key,
            self.user_id,
            resume_from_id,
        )

        # Initial ready event
        yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}

        try:
            loop = asyncio.get_event_loop()
            start_time = loop.time()
            last_heartbeat = start_time
            next_id = resume_from_id or "0-0"
            block_ms = int((heartbeat_interval / 2) * 1000)

            while True:
                # Check timeout
                now = loop.time()
                if timeout_seconds and (now - start_time) > timeout_seconds:
                    yield {
                        "event": "timeout",
                        "data": json.dumps(
                            {
                                "message": "Stream timeout",
                                "job_id": self.job_id,
                            }
                        ),
                    }
                    return

                # Send heartbeat if needed
                if (now - last_heartbeat) >= heartbeat_interval:
                    last_heartbeat = now
                    yield {"event": "ping", "data": json.dumps({"t": int(now)})}

                entries = await self._streams.xread(
                    self.stream_key,
                    next_id,
                    block_ms=block_ms,
                    count=50,
                )
                if not entries:
                    continue

                for entry_id, frame in entries:
                    next_id = entry_id
                    # Send frame event (includes job_type from the worker)
                    yield {"id": entry_id, "event": "frame", "data": json.dumps(frame)}

                    # Check for completion
                    if frame.get("type") == "finalize":
                        yield {
                            "event": "complete",
                            "data": json.dumps({"job_id": self.job_id}),
                        }
                        await self._cleanup_stream()
                        return

        except asyncio.CancelledError:
            # Client disconnected - this is expected, log at debug level
            logger.debug(
                "Deal job SSE client disconnected org=%s job=%s",
                self.organisation_id,
                self.job_id,
            )
            raise
        except Exception as e:
            logger.error("Deal job SSE stream error: %s", e, exc_info=True)
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "message": "Internal stream error",
                        "reconnect": True,
                    }
                ),
            }
        finally:
            await self.close()

    async def ensure_user_access(self, deal_id: UUID | None = None) -> bool | None:
        """Check whether the current user can access this job.

        If a route is deal-scoped, pass the requested deal_id so a valid user
        cannot subscribe to one of their jobs through another deal URL.

        Returns:
            True: Job exists and belongs to the current user and requested deal.
            False: Job exists but belongs to a different user or deal.
            None: Job missing or status check unavailable.
        """
        try:
            async for session in acquire_org_async_session(UUID(self.organisation_id)):
                job = await session.get(DealJobs, UUID(self.job_id))
                if not job:
                    return None
                if str(job.user_id) != self.user_id:
                    return False
                return deal_id is None or job.deal_id == deal_id
        except Exception:
            logger.debug("Failed to validate deal job access", exc_info=True)
        return None

    async def _get_job_status(self) -> tuple[JobStatus | None, dict | None, bool]:
        """Get current job status and result from database."""
        try:
            async for session in acquire_org_async_session(UUID(self.organisation_id)):
                job = await session.get(DealJobs, UUID(self.job_id))
                if not job:
                    return None, None, False
                if str(job.user_id) != self.user_id:
                    return None, None, True
                result_data = job.result_data
                if job.status == JobStatus.FAILED and job.error_message:
                    result_data = {"error": job.error_message}
                return job.status, result_data, False
            return None, None, False
        except Exception:
            logger.debug("Failed to check job status", exc_info=True)
            return None, None, False

    async def _cleanup_stream(self) -> None:
        """Delete the per-job Redis stream after completion."""
        try:
            if self._streams:
                await self._streams.delete_stream(self.stream_key)
                logger.info("Cleaned up deal job stream: %s", self.stream_key)
        except Exception as e:
            logger.warning("Failed to cleanup stream %s: %s", self.stream_key, e)
