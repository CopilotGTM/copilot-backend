"""Publish transient deal-job progress frames for the SSE read path.

Celery workers and agent workflows use ``DealJobRedisPublisher`` alongside the
durable ``DealJobs`` row. Redis Streams provide live progress and short replay
windows; the database remains the source of truth for terminal status and
result recovery when a stream frame is missing.
"""

from __future__ import annotations

import time
from uuid import UUID

from src.infra.messaging.redis_channels import channel_for_deal_job
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

# These wire values are shared with the frontend/chat streaming conventions.
# Changing them is a client contract change, not an internal refactor.
FRAME_TYPE_PROGRESS = "progress.update"
FRAME_TYPE_ANSWER_REPLACE = "answer.replace"
FRAME_TYPE_ERROR = "error"
FRAME_TYPE_FINALIZE = "finalize"
FRAME_TYPE_DEAL_STARTED = "import.deal.started"
FRAME_TYPE_DEAL_PROGRESS = "import.deal.progress"
FRAME_TYPE_DEAL_COMPLETED = "import.deal.completed"
FRAME_TYPE_DEAL_FAILED = "import.deal.failed"
FRAME_TYPE_SUMMARY_TICK = "import.summary.tick"


class DealJobRedisPublisher:
    """Celery tasks append live frames through this tenant/job-scoped stream writer.

    Publishers never own durable job state. Most append failures are logged and
    returned as ``None`` by ``xadd_json_frame``, but initial Redis client creation
    can still raise to the worker. SSE reconnect uses Redis entry IDs for live
    replay and the ``DealJobs`` row for terminal recovery.
    """

    def __init__(self, organisation_id: UUID, job_id: UUID, job_type: str, maxlen: int = 100):
        """Bind one stream and its approximate retained-frame limit.

        ``maxlen`` approximately limits retained entries per stream, while the
        producer also renews the shared stream TTL. It does not cap frame bytes
        or total Redis memory, so terminal recovery stays database-backed.
        """
        self._stream_key = channel_for_deal_job(organisation_id, job_id)
        self.organisation_id = organisation_id
        self._job_type = job_type
        self._job_id = str(job_id)
        self._maxlen = maxlen
        self._sequence = 0

    def publish(self, frame: dict) -> None:
        """Add transport metadata and append one frame.

        The input mapping is mutated in place. ``sequence`` is local to this
        publisher instance and may restart on worker retry; Redis entry IDs, not
        this counter, are the SSE resume cursor.
        """
        frame["sequence"] = self._sequence
        frame["timestamp"] = time.time()
        frame["job_type"] = self._job_type
        frame["job_id"] = self._job_id
        self._sequence += 1
        # Append-time failures return None and are logged. Initial Redis client
        # creation occurs before the helper's exception boundary and can still raise.
        if xadd_json_frame(self._stream_key, frame, maxlen=self._maxlen) is None:
            logger.warning(
                "Failed to publish frame to Redis stream",
                extra={
                    "event": "deal_job_publish_failed",
                    "organisation_id": str(self.organisation_id),
                    "job_id": self._job_id,
                    "job_type": self._job_type,
                },
            )

    def publish_progress(self, message: str, stage: str, progress: float) -> None:
        """Publish a user-visible stage update without changing durable status.

        Args:
            message: Human-readable progress message
            stage: Current processing stage identifier
            progress: Progress percentage (0.0 to 1.0)
        """
        self.publish(
            {
                "type": FRAME_TYPE_PROGRESS,
                "message": message,
                "stage": stage,
                "progress": progress,
            }
        )

    def publish_result(self, result_data: dict) -> None:
        """Replace the client's displayed result with the worker payload.

        Args:
            result_data: The job result data to send to the client
        """
        self.publish(
            {
                "type": FRAME_TYPE_ANSWER_REPLACE,
                "content": result_data,
            }
        )

    def publish_error(self, message: str, error_code: str) -> None:
        """Publish an error message.

        Callers should persist failure state as the recovery source; this frame
        is only the live notification and may be absent on reconnect.

        Args:
            message: Human-readable error message
            error_code: Machine-readable error code for frontend handling
        """
        self.publish(
            {
                "type": FRAME_TYPE_ERROR,
                "message": message,
                "error_code": error_code,
            }
        )

    def publish_finalize(self, reason: str = "done") -> None:
        """Tell a live SSE consumer to close after the terminal frames.

        Args:
            reason: Completion reason ('done' or 'error')
        """
        self.publish(
            {
                "type": FRAME_TYPE_FINALIZE,
                "reason": reason,
            }
        )

    def publish_deal_started(self, deal_index: int, row_number: int, deal_name: str | None) -> None:
        """Open progress reporting for one row in a multi-deal import."""
        self.publish(
            {
                "type": FRAME_TYPE_DEAL_STARTED,
                "deal_index": deal_index,
                "row_number": row_number,
                "deal_name": deal_name,
            }
        )

    def publish_deal_progress(
        self,
        deal_index: int,
        row_number: int,
        step: str,
        message: str,
        deal_name: str | None = None,
    ) -> None:
        """Report the current import step without implying row completion."""
        self.publish(
            {
                "type": FRAME_TYPE_DEAL_PROGRESS,
                "deal_index": deal_index,
                "row_number": row_number,
                "deal_name": deal_name,
                "step": step,
                "message": message,
            }
        )

    def publish_deal_completed(
        self,
        deal_index: int,
        row_number: int,
        outcome: str,
        deal_id: str | None,
        deal_name: str | None,
        warnings: list[dict] | None = None,
    ) -> None:
        """Report a terminal non-error outcome for one import row.

        outcome: created | updated | skipped
        """
        self.publish(
            {
                "type": FRAME_TYPE_DEAL_COMPLETED,
                "deal_index": deal_index,
                "row_number": row_number,
                "outcome": outcome,
                "deal_id": deal_id,
                "deal_name": deal_name,
                "warnings": warnings or [],
            }
        )

    def publish_deal_failed(
        self,
        deal_index: int,
        row_number: int,
        deal_name: str | None,
        error_code: str,
        message: str,
    ) -> None:
        """Report a terminal error for one row while the import continues."""
        self.publish(
            {
                "type": FRAME_TYPE_DEAL_FAILED,
                "deal_index": deal_index,
                "row_number": row_number,
                "deal_name": deal_name,
                "error_code": error_code,
                "message": message,
            }
        )

    def publish_summary_tick(
        self,
        *,
        processed: int,
        total: int,
        created: int,
        updated: int,
        skipped: int,
        failed: int,
        warnings: int,
    ) -> None:
        """Publish aggregate import totals without replaying every row payload."""
        self.publish(
            {
                "type": FRAME_TYPE_SUMMARY_TICK,
                "processed": processed,
                "total": total,
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "failed": failed,
                "warnings": warnings,
            }
        )
