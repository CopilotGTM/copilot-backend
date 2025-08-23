"""Attachment SSE streaming service.

Streams real-time attachment processing updates (status + checksum + thumbnails)
using Redis pub/sub (re-using knowledge base channel pattern with a dedicated channel name).

Differences vs KnowledgeSourceSSEService:
  * Tracks Attachment.status and checksum fields.
  * Terminal statuses defined for attachments (processed|failed).
  * Event payload keys: attachment_id, status (status), filename, mimetype,
    size_bytes, checksum, virus_scan_status, processing_error, thumbnails + timestamps.
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, Set, List, Optional, AsyncGenerator
from uuid import UUID

from sqlmodel import select, Session

from src.domain.models.organisation_tables.chat_message_models import Attachment, AttachmentStatus
from src.infra.messaging.redis_pubsub import AsyncRedisPubSub
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.api.schemas.upload_models import AttachmentEventUpdate

logger = get_logger("attachment_sse_service")

# Channel pattern (parallel to knowledge sources but distinct namespace)
ATTACHMENT_CHANNEL_TEMPLATE = "att:{org_id}:attachments"

def channel_for_attachment_updates(organisation_id: UUID | str) -> str:
    return ATTACHMENT_CHANNEL_TEMPLATE.format(org_id=organisation_id)

class AttachmentSSEError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)

class AttachmentSSEService:
    TERMINAL_STATUSES = {AttachmentStatus.COMPLETED.value, AttachmentStatus.FAILED.value}

    def __init__(self, org_session: Session, organisation_id: UUID, user_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.redis_pubsub = AsyncRedisPubSub()
        self.storage = AzureOrganisationBlobStorage(self.organisation_id)

    @property
    def session(self):  # pragma: no cover
        return self.org_session

    async def close(self):  # pragma: no cover - resource cleanup
        try:
            await self.redis_pubsub.close()
            try:
                await self.storage.close()
            except Exception:
                logger.debug("Storage close failed (Attachment SSE)", exc_info=True)
        except Exception:
            logger.debug("Redis pubsub close failed (Attachment SSE)", exc_info=True)

    async def get_initial_attachments(self, explicit_ids: Optional[Set[UUID]] = None) -> List[Attachment]:
        """
        Fetch initial attachment snapshot based on filter criteria.

        Args:
            explicit_ids: If provided, fetch only these specific attachment IDs.
                           If None, fetch all non-terminal attachments for the user.

        Returns:
            List of Attachment objects
        """

        # Get initial attachment states
        stmt = select(Attachment).where(
            Attachment.organisation_id == self.organisation_id,
            Attachment.user_id == self.user_id,
        )

        # Apply explicit ID filter if provided
        if explicit_ids:
            stmt = stmt.where(Attachment.attachment_id.in_(explicit_ids))
            attachments = self.org_session.exec(stmt).all()
            return attachments

        # Non-terminal only
        stmt = stmt.where(Attachment.status.not_in(list(self.TERMINAL_STATUSES)))
        attachments = self.org_session.exec(stmt).all()
        return attachments

    async def serialize_attachment(self, attachment: Attachment) -> Dict:
        """
        Convert Attachment to dict including dynamic SAS URLs.

        Adds:
            thumbnail_small_url / thumbnail_medium_url: SAS URLs if thumbnails exist

        SAS generation failures are swallowed (fields omitted) to avoid breaking stream.
        """
        model = AttachmentEventUpdate(
            attachment_id=str(attachment.attachment_id),
            status=attachment.status.value if hasattr(attachment.status, "value") else str(attachment.status),
            updated_at=attachment.updated_at.isoformat() if attachment.updated_at else None,
            error_message=attachment.processing_error,
            thumbnail_small_url=None,
        )
        data = model.model_dump()

        if model.status == AttachmentStatus.COMPLETED.value:
            try:
                data["thumbnail_small_url"] = await self.storage.generate_read_sas(
                    attachment.thumbnail_small_path,
                    minutes=120
                )
            except Exception:
                logger.debug("Failed generating SAS for thumbnail_small_path %s", attachment.thumbnail_small_path, exc_info=True)
        return data

    async def stream_events(
        self,
        explicit_ids: Optional[Set[UUID]] = None,
        timeout_seconds: Optional[int] = 1800,
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[Dict, None]:
        """
        Generate SSE events for attachment status updates.

        Args:
            explicit_ids: Optional set of attachment IDs to monitor
            timeout_seconds: Stream timeout (None for no timeout)
            heartbeat_interval: Seconds between heartbeat pings

        Yields:
            SSE event dictionaries with 'event' and 'data' keys
        """

        logger.info("Starting attachment SSE stream org=%s user=%s", self.organisation_id, self.user_id)
        start_time = asyncio.get_event_loop().time()
        last_heartbeat = start_time

        try:
            # Get initial attachment states
            initial = await self.get_initial_attachments(explicit_ids)
            watched_status: Dict[str, str] = {}

            if not initial:
                yield {"event": "complete", "data": json.dumps({"message": "no_attachments_to_watch"})}
                return

            serialized_list = await asyncio.gather(*[self.serialize_attachment(a) for a in initial])
            for serialized in serialized_list:
                watched_status[serialized["attachment_id"]] = serialized["status"]

            yield {"event": "status", "data": json.dumps(serialized_list)}

            # Check if all watched attachments are in terminal status
            if all(st in self.TERMINAL_STATUSES for st in watched_status.values()):
                yield {
                    "event": "complete", 
                    "data": json.dumps({"message": "all_attachments_terminal"})
                }
                return

            # Subscribe to Redis channel for attachment updates
            channel = channel_for_attachment_updates(self.organisation_id)
            # Precompute explicit id strings (if filtering)
            explicit_id_strs: Optional[Set[str]] = {str(i) for i in explicit_ids} if explicit_ids else None

            async for message in self.redis_pubsub.subscribe_to_channel(channel):
                current_time = asyncio.get_event_loop().time()

                # check for timeout
                if timeout_seconds and (current_time - start_time) > timeout_seconds:
                    yield {
                        "event": "timeout", 
                        "data": json.dumps({"message": "timeout", "reconnect": True})
                    }
                    return

                if (current_time - last_heartbeat) >= heartbeat_interval:
                    last_heartbeat = current_time
                    yield {
                        "event": "ping", 
                        "data": json.dumps({"timestamp": int(current_time)})
                    }

                # Check if the ID is in the explicit IDs set
                att_id = message.get("attachment_id")
                if not att_id:
                    continue

                # Apply explicit ID filter if provided
                if explicit_id_strs is not None and att_id not in explicit_id_strs:
                    continue

                watched_status[att_id] = message.get("status")
                yield {"event": "status", "data": json.dumps([message])}

                # Check if all watched attachments are in terminal status
                if all(status in self.TERMINAL_STATUSES for status in watched_status.values()):
                    yield {
                        "event": "complete",
                        "data": json.dumps({
                            "message": "all_attachments_terminal"
                        })
                    }
                    return
        except Exception as e:
            logger.error("Attachment SSE stream error: %s", e, exc_info=True)
            yield {"event": "error", "data": json.dumps({"message": "internal_error", "reconnect": True})}
        finally:
            try:
                await self.redis_pubsub.close()
            except Exception:
                logger.debug("Redis pubsub close failed in finally (Attachment SSE)")
