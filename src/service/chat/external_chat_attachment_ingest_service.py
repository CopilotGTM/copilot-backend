from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentSourceProvider,
    AttachmentStatus,
)
from src.infra.config.configs import chat_file_upload_config
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.service.chat.chat_attachment_manifest import AttachmentContextItem
from src.workers.chat_worker.client import send_chat_task_async

logger = get_logger(__name__)


class ExternalChatAttachmentIngestService:
    """Normalize external chat attachments into the internal Attachment pipeline."""

    def __init__(
        self,
        *,
        org_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.storage = AzureOrganisationBlobStorage(organisation_id)

    async def ingest_slack_files(
        self,
        slack_files: list[dict[str, Any]],
        *,
        bot_token: str,
    ) -> list[AttachmentContextItem]:
        timeout = aiohttp.ClientTimeout(
            total=chat_file_upload_config.BOT_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS
        )
        items: list[AttachmentContextItem] = []

        # Normalize provider files into CopilotGTM storage immediately so later turns
        # never depend on Slack URLs or workspace-scoped auth still being valid.
        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            for file_info in slack_files:
                url = file_info.get("url_private_download") or ""
                if not url:
                    continue

                filename = file_info.get("name") or "file"
                size = int(file_info.get("size") or 0)
                if size and size > chat_file_upload_config.BOT_ATTACHMENT_MAX_INGEST_BYTES:
                    logger.warning(
                        "Skipping Slack file %s: size=%s exceeds ingest limit", filename, size
                    )
                    continue

                try:
                    async with http_session.get(
                        url,
                        headers={"Authorization": f"Bearer {bot_token}"},
                    ) as response:
                        response.raise_for_status()
                        content = await response.read()
                        content_type = (
                            (file_info.get("mimetype") or response.content_type or "")
                            .lower()
                            .strip()
                        ) or "application/octet-stream"
                except Exception:
                    logger.warning("Failed to download Slack file %s", filename, exc_info=True)
                    continue

                item = await self.ingest_attachment_bytes(
                    filename=filename,
                    mimetype=content_type,
                    content=content,
                    source_provider=AttachmentSourceProvider.SLACK,
                    source_external_id=(str(file_info.get("id")) if file_info.get("id") else None),
                    attachment_metadata={
                        "provider_url": url,
                        "provider_payload": {
                            "size": size,
                            "mode": file_info.get("mode"),
                        },
                    },
                )
                items.append(item)

        return items

    async def ingest_teams_files(
        self,
        teams_files: list[dict[str, Any]],
        *,
        token_getter: Callable[[str], Awaitable[str | None]],
    ) -> list[AttachmentContextItem]:
        timeout = aiohttp.ClientTimeout(
            total=chat_file_upload_config.BOT_ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS
        )
        items: list[AttachmentContextItem] = []
        sources = {file_info.get("source") for file_info in teams_files}
        bot_token: str | None = None
        graph_token: str | None = None
        if "inline" in sources:
            bot_token = await token_getter("https://api.botframework.com/.default")
        if "onedrive" in sources:
            graph_token = await token_getter("https://graph.microsoft.com/.default")

        # Normalize provider files into CopilotGTM storage immediately so later turns
        # never depend on Teams download URLs or short-lived Graph links.
        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            for file_info in teams_files:
                filename = file_info.get("name") or "file"
                source = file_info.get("source") or "upload"
                download_url = file_info.get("download_url") or ""
                if not download_url:
                    continue

                actual_url, headers = self._resolve_teams_download(
                    source=source,
                    download_url=download_url,
                    bot_token=bot_token,
                    graph_token=graph_token,
                )
                if not actual_url:
                    logger.warning(
                        "Skipping Teams file %s: unable to resolve auth for source=%s",
                        filename,
                        source,
                    )
                    continue

                try:
                    async with http_session.get(
                        actual_url, headers=headers, allow_redirects=True
                    ) as response:
                        response.raise_for_status()
                        content = await response.read()
                        response_content_type = (response.content_type or "").lower()
                except Exception:
                    logger.warning("Failed to download Teams file %s", filename, exc_info=True)
                    continue

                if len(content) > chat_file_upload_config.BOT_ATTACHMENT_MAX_INGEST_BYTES:
                    logger.warning(
                        "Skipping Teams file %s: size=%s exceeds ingest limit",
                        filename,
                        len(content),
                    )
                    continue

                mimetype = (file_info.get("mimetype") or "").lower().strip()
                if not mimetype or mimetype == "application/octet-stream":
                    mimetype = self._guess_mimetype(
                        filename,
                        str(file_info.get("file_type") or ""),
                        response_content_type,
                    )

                item = await self.ingest_attachment_bytes(
                    filename=filename,
                    mimetype=mimetype,
                    content=content,
                    source_provider=AttachmentSourceProvider.TEAMS,
                    source_external_id=(str(file_info.get("id")) if file_info.get("id") else None),
                    attachment_metadata={
                        "provider_url": download_url,
                        "provider_source": source,
                        "content_url": file_info.get("content_url"),
                        "file_type": file_info.get("file_type"),
                    },
                )
                items.append(item)

        return items

    async def ingest_attachment_bytes(
        self,
        *,
        filename: str,
        mimetype: str,
        content: bytes,
        source_provider: AttachmentSourceProvider,
        source_external_id: str | None = None,
        attachment_metadata: dict[str, Any] | None = None,
    ) -> AttachmentContextItem:
        attachment_id = uuid4()
        blob_path = self.storage.plan_blob_path(
            user_id=self.user_id,
            attachment_id=attachment_id,
            original_filename=filename,
        )
        metadata = dict(attachment_metadata or {})
        metadata["ingested_via"] = "external_chat_attachment_ingest"

        await self.storage.upload_bytes(
            blob_path, content, content_type=mimetype or "application/octet-stream"
        )

        attachment = Attachment(
            attachment_id=attachment_id,
            organisation_id=self.organisation_id,
            user_id=self.user_id,
            source_provider=source_provider,
            source_external_id=source_external_id,
            filename=filename,
            storage_path=blob_path,
            mimetype=mimetype or "application/octet-stream",
            size_bytes=len(content),
            virus_scan_status="pending",
            status=AttachmentStatus.PROCESSING,
            attachment_metadata=metadata,
        )
        self.org_session.add(attachment)
        await self.org_session.flush()
        await self.org_session.commit()
        await self.org_session.refresh(attachment)

        try:
            # Reuse the standard post-upload pipeline so bot-ingested files get the same
            # checksum, thumbnail, and status handling as web uploads.
            await send_chat_task_async(
                "attachments.post_upload",
                args=[str(self.organisation_id), str(attachment.attachment_id)],
                queue="attachments",
                routing_key="attachments",
            )
        except Exception:
            logger.warning(
                "Failed to enqueue post-upload processing for attachment %s",
                attachment.attachment_id,
                exc_info=True,
            )

        return AttachmentContextItem(
            attachment_id=str(attachment.attachment_id),
            filename=attachment.filename,
            mimetype=attachment.mimetype,
            source_provider=getattr(
                attachment.source_provider, "value", attachment.source_provider
            ),
        )

    @staticmethod
    def _resolve_teams_download(
        *,
        source: str,
        download_url: str,
        bot_token: str | None,
        graph_token: str | None,
    ) -> tuple[str | None, dict[str, str]]:
        if source == "upload":
            return download_url, {}
        if source == "inline":
            if not bot_token:
                return None, {}
            return download_url, {"Authorization": f"Bearer {bot_token}"}
        if source == "onedrive":
            if not graph_token:
                return None, {}
            encoded = base64.urlsafe_b64encode(download_url.encode()).decode().rstrip("=")
            graph_url = f"https://graph.microsoft.com/v1.0/shares/u!{encoded}/driveItem/content"
            return graph_url, {"Authorization": f"Bearer {graph_token}"}
        return download_url, {}

    @staticmethod
    def _guess_mimetype(filename: str, file_type: str, response_content_type: str) -> str:
        import mimetypes

        if response_content_type and response_content_type != "application/octet-stream":
            return response_content_type

        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed.lower()

        ext_map = {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "txt": "text/plain",
            "csv": "text/csv",
            "json": "application/json",
            "xml": "application/xml",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "ogg": "audio/ogg",
            "m4a": "audio/mp4",
            "webm": "audio/webm",
        }
        return ext_map.get(file_type.lower(), "application/octet-stream")

    async def close(self) -> None:
        await self.storage.close()
