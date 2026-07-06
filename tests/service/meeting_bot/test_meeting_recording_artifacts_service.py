"""Tests for meeting recording artifact archival helpers."""

from __future__ import annotations

from uuid import uuid4

import pytest
from src.service.meeting_bot.artifacts_service import MeetingRecordingArtifactService


def test_safe_artifact_id_strips_unsafe_characters() -> None:
    service = MeetingRecordingArtifactService(uuid4())

    assert service.safe_artifact_id("../bad id", "fallback") == "bad_id"
    assert service.safe_artifact_id("", "fallback") == "fallback"


@pytest.mark.asyncio
async def test_archive_video_returns_original_when_download_url_missing() -> None:
    service = MeetingRecordingArtifactService(uuid4())
    artifact = {"recording_id": "r1", "video_id": "v1"}

    result = await service.archive_video_artifact(uuid4(), artifact)

    assert result == artifact
