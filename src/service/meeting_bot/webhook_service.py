"""Worker-side Recall webhook processing for meeting recording sessions.

Called from the Celery webhook task after the HTTP layer verifies the request.
This service maps raw Recall events onto the normalized session record stored in
the organisation schema. It is intentionally small and event-driven because webhook
timing is not guaranteed to be strictly ordered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.config.configs import recall_config
from src.infra.utils.logger import get_logger
from src.service.meeting_bot.artifacts_service import MeetingRecordingArtifactService
from src.service.meeting_bot.providers import RecallMeetingRecordingProvider
from src.service.meeting_bot.session_service import MeetingRecordingSessionService
from src.service.meeting_bot.summary_trigger_service import MeetingSummaryTriggerService

TRANSCRIPT_ARTIFACT_READY_EVENTS = {"transcript.done"}
VIDEO_ARTIFACT_READY_EVENTS = {"video_mixed.done"}
BOT_ENTERED_CALL_PERMISSION_EVENTS = {
    "bot.recording_permission_allowed",
    "bot.recording_permission_denied",
}

logger = get_logger(__name__)


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else value


class RecallMeetingRecordingWebhookService:
    """Apply Recall webhook events to org-scoped recording sessions.

    The service performs tenant-scoped state transitions only. Provider callbacks can
    be retried, delayed, or duplicated, so each transition is written to the same
    persisted session row.
    """

    def __init__(self, organisation_id: UUID) -> None:
        """Bind callback handling to one tenant context."""
        self.organisation_id = organisation_id
        self.session_service = MeetingRecordingSessionService(organisation_id)
        self.artifact_service = MeetingRecordingArtifactService(organisation_id)
        self.summary_trigger_service = MeetingSummaryTriggerService(organisation_id)

    @staticmethod
    def extract_organisation_id(payload: dict[str, Any]) -> str | None:
        """Recover the tenant boundary from the bot metadata we created on send."""
        metadata = ((payload.get("data") or {}).get("bot") or {}).get("metadata") or {}
        organisation_id = metadata.get("organisation_id")
        return organisation_id if isinstance(organisation_id, str) else None

    async def process_recall_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process one Recall event and persist the resulting normalized state.

        Return status is either:
        - "processed": a session row existed and was updated
        - "skipped": event is unsupported, duplicate, or not tied to a local session
        """
        event_name = str(payload.get("event") or "")
        data = payload.get("data") or {}
        bot = data.get("bot") or {}
        bot_id = bot.get("id")
        if not isinstance(bot_id, str) or not bot_id:
            return {
                "status": "skipped",
                "reason": "missing_bot_id",
                "event": event_name,
            }

        # Provider bot ids are not trusted across tenants; the service stays
        # org-scoped and looks up only rows within the recovered organisation.
        existing = await self.session_service.get_by_provider_session_id(bot_id)
        if not existing:
            return {
                "status": "skipped",
                "reason": "session_not_found",
                "event": event_name,
                "provider_session_id": bot_id,
            }

        provider_data = data.get("data") or {}
        provider_status = str(provider_data.get("code") or event_name or "")
        updated_at = self._parse_timestamp(provider_data.get("updated_at"))
        status_message = self._build_status_message(provider_data)

        # Preserve the last provider snapshot so debugging one session does not
        # require reconstructing state from logs plus multiple webhook deliveries.
        merged_payload = dict(existing.provider_payload or {})
        merged_payload["last_webhook_event_name"] = event_name
        merged_payload["last_webhook_event"] = payload
        if event_name.startswith("bot.in_call_") or event_name in (
            BOT_ENTERED_CALL_PERMISSION_EVENTS
        ):
            # This fact is monotonic: a later call-ended webhook must not make an
            # admitted bot look eligible for the single-session pre-join retry.
            merged_payload.setdefault(
                "first_in_call_at",
                (updated_at or datetime.now(UTC)).isoformat(),
            )
        if event_name == "bot.in_call_recording":
            merged_payload.setdefault(
                "first_recording_started_at",
                (updated_at or datetime.now(UTC)).isoformat(),
            )

        status = self._map_status(event_name, existing=existing)
        transcript_status = self._map_transcript_status(event_name, existing=existing)
        transcript_ready_at: datetime | None = None

        if event_name in TRANSCRIPT_ARTIFACT_READY_EVENTS | VIDEO_ARTIFACT_READY_EVENTS:
            # Only artifact completion events should trigger the provider follow-up
            # call. Recording lifecycle events update status without extra Recall I/O.
            bot_snapshot, transcript_artifact, video_artifact = await self._fetch_media_artifacts(
                bot_id
            )
            if bot_snapshot is not None:
                merged_payload["bot_snapshot"] = bot_snapshot
            if video_artifact is not None:
                video_artifact = await self.artifact_service.archive_video_artifact(
                    existing.calendar_event_id,
                    video_artifact,
                )
                merged_payload["video_artifact"] = video_artifact
                await self.session_service.mark_video_artifact_archived(
                    existing.session_id,
                    video_artifact,
                )
            if transcript_artifact is not None:
                merged_payload["transcript_artifact"] = transcript_artifact
            if event_name in TRANSCRIPT_ARTIFACT_READY_EVENTS and transcript_artifact is not None:
                transcript_status = MeetingTranscriptStatus.READY
                transcript_ready_at = updated_at or datetime.now(UTC)

        record = await self.session_service.apply_webhook_update(
            bot_id,
            provider_status=provider_status,
            provider_payload=merged_payload,
            status=status,
            transcript_status=transcript_status,
            status_message=status_message,
            last_webhook_at=updated_at,
            transcript_ready_at=transcript_ready_at,
        )
        if not record:
            return {
                "status": "skipped",
                "reason": "session_not_found",
                "event": event_name,
                "provider_session_id": bot_id,
            }

        result = {
            "status": "processed",
            "event": event_name,
            "provider_session_id": bot_id,
            "session_id": str(record.session_id),
            "organisation_id": str(self.organisation_id),
            "normalized_status": _enum_value(record.status),
            "transcript_status": _enum_value(record.transcript_status),
        }
        if event_name in TRANSCRIPT_ARTIFACT_READY_EVENTS:
            result.update(
                await self.summary_trigger_service.handle_transcript_ready(record.calendar_event_id)
            )
        logger.info(
            "Processed Recall webhook",
            extra={
                "event": "recall_webhook_processed",
                "recall_event": event_name,
                "provider_session_id": bot_id,
                "session_id": str(record.session_id),
                "organisation_id": str(self.organisation_id),
                "calendar_event_id": str(record.calendar_event_id),
                "normalized_status": _enum_value(record.status),
                "transcript_status": _enum_value(record.transcript_status),
                "transcript_materialized": result.get("transcript_materialized"),
                "summary_jobs_created": result.get("summary_jobs_created"),
                "summary_jobs_skipped": result.get("summary_jobs_skipped"),
                "transcript_materialization_error": result.get("transcript_materialization_error"),
            },
        )
        return result

    async def _fetch_media_artifacts(
        self,
        provider_session_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None, dict[str, str] | None]:
        """Fetch the latest bot payload only when downloadable artifacts should exist.

        The extra provider call is intentionally gated to artifact-ready events to
        avoid needless API usage on every webhook delivery.
        """
        api_key = await recall_config.get_api_key()
        if not api_key:
            return None, None, None

        provider = RecallMeetingRecordingProvider(
            api_key=api_key,
            region=recall_config.RECALL_REGION,
            timeout=recall_config.RECALL_TIMEOUT_SECONDS,
        )
        bot_snapshot = await provider.fetch_session(provider_session_id)
        transcript_artifact = provider.extract_transcript_download(bot_snapshot)
        video_artifact = provider.extract_video_mixed_download(bot_snapshot)
        return bot_snapshot, transcript_artifact, video_artifact

    @staticmethod
    def _map_status(
        event_name: str,
        *,
        existing: Any | None = None,
    ) -> MeetingRecordingStatus | None:
        """Map provider event names to our internal lifecycle state."""
        # Keep unknown events unopinionated so a webhook rename does not block other
        # state updates (status stays preserved unless we have a known mapping).
        if event_name in {"bot.joining_call", "bot.in_waiting_room"}:
            return MeetingRecordingStatus.JOINING
        if event_name in {
            "bot.in_call_not_recording",
            "bot.recording_permission_allowed",
            "bot.in_call_recording",
        }:
            return MeetingRecordingStatus.IN_CALL
        if event_name == "bot.recording_permission_denied":
            return MeetingRecordingStatus.FAILED
        if event_name == "bot.call_ended":
            existing_status = _enum_value(existing.status) if existing is not None else None
            if existing_status not in {
                MeetingRecordingStatus.IN_CALL.value,
                MeetingRecordingStatus.COMPLETED.value,
            }:
                return MeetingRecordingStatus.FAILED
            return MeetingRecordingStatus.COMPLETED
        if event_name == "bot.done":
            provider_payload = existing.provider_payload if existing is not None else {}
            if isinstance(provider_payload, dict) and provider_payload.get(
                "first_recording_started_at"
            ):
                return MeetingRecordingStatus.COMPLETED
            existing_status = _enum_value(existing.status) if existing is not None else None
            if existing_status == MeetingRecordingStatus.FAILED.value:
                return None
            return MeetingRecordingStatus.COMPLETED
        if event_name == "recording.done":
            return MeetingRecordingStatus.COMPLETED
        if event_name in {"bot.fatal", "recording.failed"}:
            return MeetingRecordingStatus.FAILED
        if event_name == "recording.deleted":
            return MeetingRecordingStatus.CANCELLED
        return None

    @staticmethod
    def _map_transcript_status(
        event_name: str,
        *,
        existing: Any | None = None,
    ) -> MeetingTranscriptStatus | None:
        """Map transcript lifecycle events from Recall to local status values."""
        if event_name == "transcript.processing":
            return MeetingTranscriptStatus.PENDING
        if event_name == "transcript.done":
            return MeetingTranscriptStatus.READY
        if event_name == "transcript.failed":
            return MeetingTranscriptStatus.FAILED
        if event_name == "transcript.deleted":
            return MeetingTranscriptStatus.NOT_AVAILABLE
        if event_name in {"bot.fatal", "bot.recording_permission_denied"}:
            return MeetingTranscriptStatus.FAILED
        if event_name == "bot.call_ended":
            existing_status = _enum_value(existing.status) if existing is not None else None
            if existing_status not in {
                MeetingRecordingStatus.IN_CALL.value,
                MeetingRecordingStatus.COMPLETED.value,
            }:
                return MeetingTranscriptStatus.FAILED
        if event_name == "recording.failed":
            return MeetingTranscriptStatus.FAILED
        if event_name == "recording.deleted":
            return MeetingTranscriptStatus.NOT_AVAILABLE
        return None

    @staticmethod
    def _build_status_message(provider_data: dict[str, Any]) -> str | None:
        """Prefer explicit sub-code + message for operator-level diagnostics."""
        message = provider_data.get("message")
        sub_code = provider_data.get("sub_code")
        if message and sub_code:
            return f"{sub_code}: {message}"
        if message:
            return str(message)
        if sub_code:
            return str(sub_code)
        return None

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        """Parse Recall ISO-8601 timestamps and normalize missing timezone to UTC."""
        if not isinstance(value, str) or not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
