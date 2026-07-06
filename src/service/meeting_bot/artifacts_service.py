"""Artifact storage helpers for meeting recording provider media."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from src.infra.config.configs import recall_config
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage


class MeetingRecordingArtifactService:
    """Archive provider media artifacts into org-scoped storage."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    async def archive_video_artifact(
        self,
        event_id: UUID,
        video_artifact: dict[str, str],
    ) -> dict[str, Any]:
        """Download Recall's mixed MP4 into our org-scoped blob container."""
        download_url = video_artifact.get("download_url")
        if not download_url:
            return video_artifact

        recording_id = self.safe_artifact_id(video_artifact.get("recording_id"), "recording")
        video_id = self.safe_artifact_id(video_artifact.get("video_id"), "video")
        blob_path = f"meeting_recordings/{event_id}/{recording_id}/{video_id}.mp4"
        storage = AzureOrganisationBlobStorage(self.organisation_id)
        blob_url = (
            f"https://{storage.account_name}.blob.core.windows.net/{storage.container}/{blob_path}"
        )
        try:
            async with (
                httpx.AsyncClient(timeout=recall_config.RECALL_TIMEOUT_SECONDS) as client,
                client.stream("GET", download_url) as response,
            ):
                response.raise_for_status()
                content_type = response.headers.get("content-type") or "video/mp4"
                await storage.upload_stream(
                    blob_path,
                    response.aiter_bytes(),
                    content_type=content_type,
                )
                result: dict[str, Any] = {
                    **video_artifact,
                    "archived_blob_path": blob_path,
                    "archived_blob_url": blob_url,
                    "archived_content_type": content_type,
                    "archived_at": datetime.now(UTC).isoformat(),
                }
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit():
                    result["archived_size_bytes"] = int(content_length)
                return result
        finally:
            await storage.close()

    @staticmethod
    def safe_artifact_id(value: Any, fallback: str) -> str:
        """Normalize provider ids before using them in blob paths."""
        text = str(value or fallback)
        return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-") or fallback
