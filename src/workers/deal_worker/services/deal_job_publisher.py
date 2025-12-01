"""Redis publisher for deal job progress and results.

This module provides a publisher for streaming job progress and results
to clients via Redis Streams, consumed by SSE endpoints.
"""

from __future__ import annotations

import time
from uuid import UUID

from src.infra.messaging.redis_channels import channel_for_deal_job
from src.infra.messaging.redis_streams import xadd_json_frame
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

# Frame type constants (matching chat system conventions)
FRAME_TYPE_PROGRESS = "progress.update"
FRAME_TYPE_ANSWER_REPLACE = "answer.replace"
FRAME_TYPE_ERROR = "error"
FRAME_TYPE_FINALIZE = "finalize"


class DealJobRedisPublisher:
    """Publish JSON frames to a Redis Stream for any deal job.

    This publisher is used by Celery tasks to stream progress updates
    and results back to clients via SSE.
    """

    def __init__(self, organisation_id: UUID, job_id: UUID, job_type: str, maxlen: int = 100):
        self._stream_key = channel_for_deal_job(organisation_id, job_id)
        self._job_type = job_type
        self._job_id = str(job_id)
        self._maxlen = maxlen
        self._sequence = 0

    def publish(self, frame: dict) -> None:
        """Publish a frame to the Redis stream."""
        frame["sequence"] = self._sequence
        frame["timestamp"] = time.time()
        frame["job_type"] = self._job_type
        frame["job_id"] = self._job_id
        self._sequence += 1
        if xadd_json_frame(self._stream_key, frame, maxlen=self._maxlen) is None:
            logger.warning("Failed to XADD frame for job %s", self._job_id)

    def publish_progress(self, message: str, stage: str, progress: float) -> None:
        """Publish a progress update.

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
        """Publish the final result.

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

        Use this for user-facing errors that explain why the job failed.

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
        """Publish completion signal.

        Args:
            reason: Completion reason ('done' or 'error')
        """
        self.publish(
            {
                "type": FRAME_TYPE_FINALIZE,
                "reason": reason,
            }
        )
