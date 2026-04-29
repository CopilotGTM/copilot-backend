"""Worker-side Recall webhook processing for meeting recording sessions.

Called from the Celery webhook task after the HTTP layer verifies the request.
This service maps raw Recall events onto the normalized session record stored in
the organisation schema. It is intentionally small and event-driven because webhook
timing is not guaranteed to be strictly ordered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)
from src.infra.config.configs import recall_config
from src.service.integrations.meeting_recording.providers import RecallMeetingRecordingProvider
from src.service.meeting_bot.session_service import MeetingRecordingSessionService

TRANSCRIPT_ARTIFACT_READY_EVENTS = {"transcript.done"}


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

        status = self._map_status(event_name)
        transcript_status = self._map_transcript_status(event_name)
        transcript_ready_at: datetime | None = None

        if event_name in TRANSCRIPT_ARTIFACT_READY_EVENTS:
            # Only transcript completion should trigger the provider follow-up call.
            # Recording lifecycle events update status only; transcript availability
            # remains owned by Recall's transcript artifact lifecycle.
            bot_snapshot, transcript_artifact = await self._fetch_transcript_artifact(bot_id)
            if bot_snapshot is not None:
                merged_payload["bot_snapshot"] = bot_snapshot
            if transcript_artifact is not None:
                merged_payload["transcript_artifact"] = transcript_artifact
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

        return {
            "status": "processed",
            "event": event_name,
            "provider_session_id": bot_id,
            "session_id": str(record.session_id),
            "organisation_id": str(self.organisation_id),
            "normalized_status": record.status.value,
            "transcript_status": record.transcript_status.value,
        }

    async def _fetch_transcript_artifact(
        self,
        provider_session_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        """Fetch the latest bot payload only when transcript artifacts should exist.

        The extra provider call is intentionally gated to transcript-ready events to
        avoid needless API usage on every webhook delivery.
        """
        api_key = await recall_config.get_api_key()
        if not api_key:
            return None, None

        provider = RecallMeetingRecordingProvider(
            api_key=api_key,
            region=recall_config.RECALL_REGION,
            timeout=recall_config.RECALL_TIMEOUT_SECONDS,
        )
        bot_snapshot = await provider.fetch_session(provider_session_id)
        transcript_artifact = provider.extract_transcript_download(bot_snapshot)
        return bot_snapshot, transcript_artifact

    @staticmethod
    def _map_status(event_name: str) -> MeetingRecordingStatus | None:
        """Map provider event names to our internal lifecycle state."""
        # Keep unknown events unopinionated so a webhook rename does not block other
        # state updates (status stays preserved unless we have a known mapping).
        if event_name in {"bot.joining_call", "bot.in_waiting_room"}:
            return MeetingRecordingStatus.JOINING
        if event_name in {
            "bot.in_call_not_recording",
            "bot.recording_permission_allowed",
            "bot.recording_permission_denied",
            "bot.in_call_recording",
        }:
            return MeetingRecordingStatus.IN_CALL
        if event_name in {"bot.call_ended", "bot.done", "recording.done"}:
            return MeetingRecordingStatus.COMPLETED
        if event_name in {"bot.fatal", "recording.failed"}:
            return MeetingRecordingStatus.FAILED
        if event_name == "recording.deleted":
            return MeetingRecordingStatus.CANCELLED
        return None

    @staticmethod
    def _map_transcript_status(event_name: str) -> MeetingTranscriptStatus | None:
        """Map transcript lifecycle events from Recall to local status values."""
        if event_name == "transcript.processing":
            return MeetingTranscriptStatus.PENDING
        if event_name == "transcript.done":
            return MeetingTranscriptStatus.READY
        if event_name == "transcript.failed":
            return MeetingTranscriptStatus.FAILED
        if event_name == "transcript.deleted":
            return MeetingTranscriptStatus.NOT_AVAILABLE
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
