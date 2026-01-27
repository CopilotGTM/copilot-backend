"""Organisation job SSE streaming service.

Provides Server-Sent Events streaming for organisation job status updates
using Redis pub/sub for real-time notifications. Handles connection lifecycle,
timeouts, and error recovery.

Organisation jobs are master-level entities (not tenant-scoped), so this service
operates on the master database.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from src.api.schemas.admin_job_models import OrganisationJobEventUpdate
from src.domain.models.master_tables.organisation_job_models import (
    OrganisationJob,
    OrganisationJobStatus,
)
from src.infra.database.sessions import master_async_session_scope
from src.infra.messaging.redis_channels import channel_for_org_job
from src.infra.messaging.redis_pubsub import AsyncRedisPubSub
from src.infra.utils.logger import get_logger
from src.service.admin.organisation_job_service import organisation_job_service

logger = get_logger("organisation_job_sse_service")


class OrganisationJobSSEService:
    """Service for streaming organisation job status via SSE with Redis pub/sub.

    Notes:
        * Acquires a short-lived master database session internally (public schema).
        * Validates job ownership by admin_id before streaming.
    """

    TERMINAL_STATUSES = {
        OrganisationJobStatus.COMPLETED,
        OrganisationJobStatus.FAILED,
        OrganisationJobStatus.CANCELLED,
    }

    def __init__(self, admin_id: UUID):
        """Initialize SSE service.

        Args:
            admin_id: Admin user ID for job ownership validation
        """
        self.admin_id = admin_id
        self.redis_pubsub = AsyncRedisPubSub()

    async def close(self):
        """Cleanup resources."""
        try:
            await self.redis_pubsub.close()
        except Exception:
            logger.debug("Redis pubsub close failed (SSE)", exc_info=True)

    def _send_event(self, event_type: str, data) -> dict:
        """Helper to format SSE event as dict."""
        return {"event": event_type, "data": json.dumps(data)}

    def _serialize_job(self, job: OrganisationJob) -> dict:
        """Convert OrganisationJob to SSE event payload."""
        event = OrganisationJobEventUpdate(
            job_id=job.job_id,
            status=job.status,
            progress=job.progress,
            status_message=job.status_message,
            result_data=job.result_data,
            error_message=job.error_message,
            error_code=job.error_code,
            updated_at=job.updated_at,
            completed_at=getattr(job, "completed_at", None),
        )
        return event.model_dump(mode="json")

    def _is_terminal(self, status: OrganisationJobStatus) -> bool:
        """Check if status is terminal."""
        return status in self.TERMINAL_STATUSES

    async def stream_job_events(
        self,
        job_id: UUID,
        timeout_seconds: int | None = 600,
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[dict, None]:
        """
        Generate SSE events for organisation job status updates.

        Args:
            job_id: Job UUID to stream
            timeout_seconds: Stream timeout (default 10 minutes)
            heartbeat_interval: Seconds between heartbeat pings

        Yields:
            SSE event dicts with 'event' and 'data' keys

        Event types:
            - status: Job status update
            - complete: Job reached terminal state
            - ping: Heartbeat
            - error: Stream error
            - timeout: Stream timeout
        """
        logger.info(f"Starting SSE stream for job {job_id}, admin {self.admin_id}")
        start_time = asyncio.get_event_loop().time()
        last_heartbeat = start_time

        try:
            # Fetch initial job state and validate ownership
            async with master_async_session_scope() as session:
                job = await organisation_job_service.get_job_by_admin(
                    session, job_id, self.admin_id
                )

            if not job:
                yield self._send_event(
                    "error",
                    {"message": "Job not found or access denied", "code": "NOT_FOUND"},
                )
                return

            # Send initial status
            yield self._send_event("status", self._serialize_job(job))

            # If already terminal, complete immediately
            if self._is_terminal(job.status):
                yield self._send_event("complete", {"message": "job_already_terminal"})
                return

            # Stream updates from Redis
            async for event in self._stream_redis_updates(
                job_id,
                start_time,
                last_heartbeat,
                timeout_seconds,
                heartbeat_interval,
            ):
                yield event

        except Exception as e:
            logger.error(f"SSE stream error for job {job_id}: {e}", exc_info=True)
            yield self._send_event(
                "error",
                {
                    "message": "Internal stream error. Please reconnect.",
                    "error_type": type(e).__name__,
                    "reconnect": True,
                },
            )
        finally:
            try:
                await self.redis_pubsub.close()
            except Exception:
                logger.debug("Redis pubsub close failed in finally (SSE)")

    async def _stream_redis_updates(
        self,
        job_id: UUID,
        start_time: float,
        last_heartbeat: float,
        timeout_seconds: int | None,
        heartbeat_interval: float,
    ) -> AsyncGenerator[dict, None]:
        """Stream updates from Redis pub/sub.

        Uses polling so we can emit heartbeat pings even when no Redis messages
        arrive for a while (e.g., migrations step).
        """
        channel = channel_for_org_job(job_id)

        async for message in self.redis_pubsub.subscribe_and_poll(
            channel,
            poll_timeout=min(heartbeat_interval / 2, 5.0),
        ):
            current_time = asyncio.get_event_loop().time()

            # Check timeout
            if timeout_seconds and (current_time - start_time) > timeout_seconds:
                yield self._send_event(
                    "timeout",
                    {
                        "message": "Stream timeout reached. Please reconnect.",
                        "timeout_seconds": timeout_seconds,
                        "reconnect": True,
                    },
                )
                return

            # Send heartbeat if needed
            if (current_time - last_heartbeat) >= heartbeat_interval:
                last_heartbeat = current_time
                yield self._send_event("ping", {"timestamp": int(current_time)})

            if message is None:
                continue

            # Yield status update
            yield self._send_event("status", message)

            # Check if terminal
            status_str = message.get("status", "")
            try:
                status = OrganisationJobStatus(status_str)
                if self._is_terminal(status):
                    yield self._send_event("complete", {"message": "job_completed"})
                    return
            except ValueError:
                # Unknown status, continue streaming
                pass
