"""User-facing meeting recording status projection."""

from __future__ import annotations

from enum import Enum
from typing import Any

from src.domain.models.organisation_tables.sync.meeting_recording import (
    MeetingRecordingStatus,
    MeetingTranscriptStatus,
)


def meeting_recording_display_status(
    *,
    recording_status: Any,
    transcript_status: Any = None,
    status_message: str | None = None,
    sent_to_provider: bool = False,
) -> str:
    """Return the semantic status code clients should render.

    copilotgtm: This is intentionally a small heuristic over our normalized DB
    state. If Recall adds stable failure classes, replace the substring checks
    with an explicit provider-code mapping at webhook ingestion time.
    """
    status = _status_value(recording_status)
    transcript = _status_value(transcript_status)
    reason = (status_message or "").lower()

    if status is None:
        return "not_requested"
    if status == MeetingRecordingStatus.CANCELLED.value:
        return "bot_removed"
    if status in {
        MeetingRecordingStatus.PENDING_SEND.value,
        MeetingRecordingStatus.SEND_QUEUED.value,
    }:
        return "bot_queued"
    if status == MeetingRecordingStatus.SENT.value:
        return "bot_invited" if sent_to_provider else "bot_queued"
    if status in {
        MeetingRecordingStatus.JOINING.value,
        MeetingRecordingStatus.IN_CALL.value,
    }:
        return "recording_live"
    if status == MeetingRecordingStatus.COMPLETED.value:
        if transcript == MeetingTranscriptStatus.FAILED.value:
            return "transcript_failed"
        return "recording_available"
    if status == MeetingRecordingStatus.FAILED.value:
        if _contains_any(reason, ("host_denied", "recording_permission_denied")):
            return "recording_permission_denied"
        if _contains_any(reason, ("recording_unavailable", "recording failed")):
            return "recording_failed"
        if _contains_any(
            reason,
            (
                "meeting_not_accessible",
                "bot_kicked",
                "waiting_room",
                "call_ended",
                "cannot_join",
                "did_not_join",
            ),
        ):
            return "bot_did_not_join"
        return "bot_failed"
    if transcript == MeetingTranscriptStatus.FAILED.value:
        return "transcript_failed"
    return "not_available"


def _status_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)
