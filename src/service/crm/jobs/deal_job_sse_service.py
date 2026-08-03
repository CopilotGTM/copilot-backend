"""Authorize and stream user-visible deal-job events over SSE.

Deal and meeting routers validate access through this service before returning
an ``EventSourceResponse``. Terminal state is replayed from the tenant database;
pending/in-progress jobs read best-effort frames from a per-job Redis Stream and
resume with the client's Redis ``Last-Event-ID``.

The database is authoritative because Redis retention is intentionally short.
A missing progress frame must not prevent a completed, failed, or cancelled job
from producing a terminal response after reconnect.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.redis_channels import channel_for_deal_job
from src.infra.messaging.redis_streams import AsyncRedisStreams
from src.infra.utils.logger import get_logger

logger = get_logger("deal_job_sse_service")

FRAME_TYPE_ANSWER_REPLACE = "answer.replace"
FRAME_TYPE_FINALIZE = "finalize"


class DealJobSSEService:
    """Stream one tenant-local job with database-backed recovery.

    Contact research is user-owned. A deal or meeting router may grant shared
    access only after matching the job's deal, event, and allowed job type. That
    successful route precheck is repeated for durable result reads and forms the
    Redis security boundary.
    """

    def __init__(
        self,
        organisation_id: UUID,
        job_id: UUID,
        user_id: UUID,
    ) -> None:
        """Bind the stream key and default to user-owned durable reads."""
        self.organisation_id = str(organisation_id)
        self.job_id = str(job_id)
        self.user_id = str(user_id)
        self._shared_scope: tuple[UUID, UUID | None, frozenset[DealJobType]] | None = None
        self.stream_key = channel_for_deal_job(self.organisation_id, self.job_id)
        self._streams: AsyncRedisStreams | None = None

    async def close(self) -> None:
        """Close the lazily opened Redis client without masking request completion."""
        try:
            if self._streams:
                await self._streams.close()
        except Exception:
            # Cleanup failure is operational noise; the SSE generator is already ending.
            logger.debug("Redis streams close failed (deal job SSE)", exc_info=True)

    async def stream_events(
        self,
        *,
        resume_from_id: str | None = None,
        timeout_seconds: int = 300,  # 5 min timeout
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[dict, None]:
        """Yield recovered terminal state or live Redis frames for one job.

        Completed/failed/cancelled rows return without opening Redis. Active jobs
        begin at ``resume_from_id`` or replay the retained stream from ``0-0``.
        Heartbeats keep intermediaries from closing an otherwise quiet request;
        timeout ends this HTTP stream but does not cancel the durable job.

        Args:
            timeout_seconds: Max time before timing out the stream
            heartbeat_interval: Time between heartbeat pings
        """

        # Read durable state before Redis so reconnect after stream trimming still
        # returns terminal output and repeats the user-ownership check.
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
            # Failure detail may live in result_data or the durable error_message
            # normalized by _get_job_status.
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
            # Cancellation is terminal but uses the error event shape expected by
            # current clients, followed by complete so they close the connection.
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

        # Pending/in-progress work needs the transient Redis channel for live frames.
        self._streams = AsyncRedisStreams()

        logger.info(
            "Deal job SSE connect org=%s job=%s stream=%s user=%s resume_from=%s",
            self.organisation_id,
            self.job_id,
            self.stream_key,
            self.user_id,
            resume_from_id,
        )

        # Emit readiness before a blocking Redis read so clients know subscription succeeded.
        yield {"event": "ready", "data": json.dumps({"job_id": self.job_id})}

        try:
            loop = asyncio.get_event_loop()
            start_time = loop.time()
            last_heartbeat = start_time
            next_id = resume_from_id or "0-0"
            # Wake twice per heartbeat interval even without frames, leaving enough
            # time to send a ping before common proxy idle timeouts.
            block_ms = int((heartbeat_interval / 2) * 1000)

            while True:
                # Timeout governs only this HTTP connection; the worker keeps running.
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

                # Heartbeats are SSE liveness signals and are not stored in Redis.
                if (now - last_heartbeat) >= heartbeat_interval:
                    last_heartbeat = now
                    yield {"event": "ping", "data": json.dumps({"t": int(now)})}

                entries = await self._streams.xread(
                    self.stream_key,
                    next_id,
                    block_ms=block_ms,
                    # Bound each response batch so one noisy stream cannot monopolize
                    # the event loop before pending heartbeats/timeouts are rechecked.
                    count=50,
                )
                if not entries:
                    continue

                for entry_id, frame in entries:
                    # Advance after every delivered Redis entry so Last-Event-ID resumes
                    # strictly after the last frame observed by this generator.
                    next_id = entry_id
                    # Worker frames already include job type and publisher metadata.
                    yield {"id": entry_id, "event": "frame", "data": json.dumps(frame)}

                    # Finalize is the live-stream terminal marker; durable recovery uses DB status.
                    if frame.get("type") == "finalize":
                        yield {
                            "event": "complete",
                            "data": json.dumps({"job_id": self.job_id}),
                        }
                        return

        except asyncio.CancelledError:
            # Client disconnect is normal control flow and must propagate so Starlette
            # can cancel the response promptly.
            logger.debug(
                "Deal job SSE client disconnected org=%s job=%s",
                self.organisation_id,
                self.job_id,
            )
            raise
        except Exception as e:
            # A transient Redis/read failure invites client reconnect; durable job
            # status remains unchanged and will be checked again on the next request.
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
        cannot subscribe to one of their jobs through another deal URL. The
        organisation-specific session is the tenant boundary for the job UUID.

        Returns:
            True: Job exists and belongs to the current user and requested deal.
            False: Job exists but belongs to a different user or deal.
            None: Job missing or status check unavailable.
        """
        try:
            async for session in acquire_org_async_session(UUID(self.organisation_id)):
                job = await self._get_job(session)
                if not job:
                    return None
                if str(job.user_id) != self.user_id:
                    return False
                return deal_id is None or job.deal_id == deal_id
        except Exception:
            # ``None`` deliberately makes routers fail closed as not found when the
            # ownership check is unavailable.
            logger.debug("Failed to validate deal job access", exc_info=True)
        return None

    async def ensure_deal_access(
        self,
        deal_id: UUID,
        *,
        event_id: UUID | None,
        allowed_job_types: frozenset[DealJobType],
    ) -> bool | None:
        """Match a shared job to the complete route-authorized identity.

        The router separately enforces the caller's current deal access. This
        check prevents a valid collaborator from substituting another deal's,
        meeting's, or job type's ID in the stream URL.
        """
        self._shared_scope = None
        try:
            async for session in acquire_org_async_session(UUID(self.organisation_id)):
                job = await self._get_job(session)
                if not job:
                    return None
                scope = (deal_id, event_id, allowed_job_types)
                if not self._matches_deal_scope(job, scope):
                    return False
                self._shared_scope = scope
                return True
        except Exception:
            logger.debug("Failed to validate deal-scoped job access", exc_info=True)
        return None

    async def _get_job_status(self) -> tuple[JobStatus | None, dict | None, bool]:
        """Load durable status/result and repeat the configured ownership check.

        User-owned streams fail closed on an owner mismatch. Deal-scoped streams
        may return another collaborator's shared result only after the router has
        enforced deal access and ``ensure_deal_access`` has matched the job URL.
        """
        try:
            async for session in acquire_org_async_session(UUID(self.organisation_id)):
                job = await self._get_job(session)
                if not job:
                    return None, None, False
                if self._shared_scope is not None:
                    if not self._matches_deal_scope(job, self._shared_scope):
                        return None, None, True
                elif str(job.user_id) != self.user_id:
                    return None, None, True
                result_data = job.result_data
                # Workers persist the human-readable failure separately from normal
                # result payloads; normalize it for the terminal SSE branch.
                if job.status == JobStatus.FAILED and job.error_message:
                    result_data = {"error": job.error_message}
                return job.status, result_data, False
            return None, None, False
        except Exception:
            # Live progress may remain available during a transient DB read failure.
            logger.debug("Failed to check job status", exc_info=True)
            return None, None, False

    async def _get_job(self, session: AsyncSession) -> DealJobs | None:
        stmt = select(DealJobs).where(
            col(DealJobs.organisation_id) == UUID(self.organisation_id),
            col(DealJobs.job_id) == UUID(self.job_id),
        )
        return (await session.execute(stmt)).scalars().first()

    @staticmethod
    def _matches_deal_scope(
        job: DealJobs,
        scope: tuple[UUID, UUID | None, frozenset[DealJobType]],
    ) -> bool:
        deal_id, event_id, allowed_job_types = scope
        return (
            job.deal_id == deal_id
            and job.event_id == event_id
            and job.job_type in allowed_job_types
        )
