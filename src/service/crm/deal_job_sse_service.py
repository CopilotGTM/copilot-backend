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
        self.consumer_group = f"sse_consumers_{self.job_id}"
        self.consumer_name = f"user_{self.user_id}"
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
        timeout_seconds: int = 300,  # 5 min timeout
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[dict, None]:
        """
        Yield SSE events for any deal job from Redis Stream.

        Handles the following edge cases:
        1. Job already completed - returns cached result immediately
        2. Job in progress - streams events from Redis
        3. User reconnects mid-job - resumes from where they left off (consumer group)

        Args:
            timeout_seconds: Max time before timing out the stream
            heartbeat_interval: Time between heartbeat pings
        """

        # Check job status first (handles reconnection scenario)
        job_status, result_data = await self._get_job_status()

        if job_status == JobStatus.COMPLETED:
            # Job finished before or during disconnect - send cached result
            logger.info(
                "Deal job SSE: job already completed org=%s job=%s",
                self.organisation_id,
                self.job_id,
            )
            yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}
            if result_data:
                yield {
                    "event": "frame",
                    "data": json.dumps(
                        {
                            "type": "answer.replace",
                            "job_id": self.job_id,
                            "content": result_data,
                        }
                    ),
                }
            yield {"event": "complete", "data": json.dumps({"job_id": self.job_id})}
            return

        if job_status == JobStatus.FAILED:
            logger.info(
                "Deal job SSE: job failed org=%s job=%s",
                self.organisation_id,
                self.job_id,
            )
            yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "job_id": self.job_id,
                        "message": "Job failed",
                        "reconnect": False,
                    }
                ),
            }
            return

        # Job is pending or in progress - stream from Redis
        self._streams = AsyncRedisStreams()

        logger.info(
            "Deal job SSE connect org=%s job=%s stream=%s user=%s",
            self.organisation_id,
            self.job_id,
            self.stream_key,
            self.user_id,
        )

        # Initial ready event
        yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}

        try:
            loop = asyncio.get_event_loop()
            start_time = loop.time()
            last_heartbeat = start_time

            async for entry_id, frame in self._streams.read_forever_consumer_group(
                self.stream_key,
                self.consumer_group,
                self.consumer_name,
                auto_ack=True,
                block_ms=int((heartbeat_interval / 2) * 1000),
                count=50,
            ):
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

    async def _get_job_status(self) -> tuple[JobStatus | None, dict | None]:
        """Get current job status and result from database."""
        try:
            async for session in acquire_org_async_session(UUID(self.organisation_id)):
                job = await session.get(DealJobs, UUID(self.job_id))
                if not job:
                    return None, None
                return job.status, job.result_data
        except Exception:
            logger.debug("Failed to check job status", exc_info=True)
            return None, None

    async def _cleanup_stream(self) -> None:
        """Clean up Redis stream after completion."""
        try:
            if self._streams:
                await self._streams.delete_stream(self.stream_key)
                logger.info("Cleaned up deal job stream: %s", self.stream_key)
        except Exception as e:
            logger.warning("Failed to cleanup stream %s: %s", self.stream_key, e)
