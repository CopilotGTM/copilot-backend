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
        stmt = select(Attachment).where(
            Attachment.organisation_id == self.organisation_id,
            Attachment.user_id == self.user_id,
        )
        if explicit_ids:
            stmt = stmt.where(Attachment.attachment_id.in_(explicit_ids))
            return self.org_session.exec(stmt).all()
        # Non-terminal only
        stmt = stmt.where(Attachment.status.not_in(list(self.TERMINAL_STATUSES)))  # type: ignore[attr-defined]
        return self.org_session.exec(stmt).all()

    async def serialize_attachment(self, attachment: Attachment) -> Dict:
        data: Dict[str, object] = {
            "attachment_id": str(attachment.attachment_id),
            "status": attachment.status,
            "filename": attachment.filename,
            "mimetype": attachment.mimetype,
            "size_bytes": attachment.size_bytes,
            "checksum": attachment.checksum,
            "virus_scan_status": attachment.virus_scan_status,
            "processing_error": attachment.processing_error,
            "updated_at": attachment.updated_at.isoformat() if attachment.updated_at else None,
            "created_at": attachment.created_at.isoformat() if attachment.created_at else None,
        }
        try:
            if getattr(attachment, "thumbnail_small_path", None):
                data["thumbnail_small_url"] = await self.storage.generate_read_sas(
                    attachment.thumbnail_small_path, minutes=120
                )
            if getattr(attachment, "thumbnail_medium_path", None):
                data["thumbnail_medium_url"] = await self.storage.generate_read_sas(
                    attachment.thumbnail_medium_path, minutes=120
                )
        except Exception:
            logger.debug("Failed generating thumbnail SAS for attachment %s", attachment.attachment_id, exc_info=True)
        return data

    async def stream_events(
        self,
        explicit_ids: Optional[Set[UUID]] = None,
        timeout_seconds: Optional[int] = 1800,
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[Dict, None]:
        logger.info("Starting attachment SSE stream org=%s user=%s", self.organisation_id, self.user_id)
        start_time = asyncio.get_event_loop().time()
        last_heartbeat = start_time
        try:
            initial = await self.get_initial_attachments(explicit_ids)
            tracked: Dict[str, str] = {}
            if not initial:
                yield {"event": "complete", "data": json.dumps({"message": "no_attachments_to_watch"})}
                return
            serialized = await asyncio.gather(*[self.serialize_attachment(a) for a in initial])
            for s in serialized:
                tracked[s["attachment_id"]] = s["status"]  # type: ignore[index]
            yield {"event": "snapshot", "data": json.dumps(serialized)}
            if all(st in self.TERMINAL_STATUSES for st in tracked.values()):
                yield {"event": "complete", "data": json.dumps({"message": "all_attachments_terminal"})}
                return

            channel = channel_for_attachment_updates(self.organisation_id)
            async for message in self.redis_pubsub.subscribe_to_channel(channel):
                now = asyncio.get_event_loop().time()
                if timeout_seconds and (now - start_time) > timeout_seconds:
                    yield {"event": "timeout", "data": json.dumps({"message": "timeout", "reconnect": True})}
                    return
                if (now - last_heartbeat) >= heartbeat_interval:
                    last_heartbeat = now
                    yield {"event": "ping", "data": json.dumps({"timestamp": int(now)})}
                att_id = message.get("attachment_id")
                if not att_id:
                    continue
                if explicit_ids and att_id not in {str(i) for i in explicit_ids}:
                    continue
                new_status = message.get("status")
                if att_id in tracked:
                    if tracked[att_id] != new_status:
                        tracked[att_id] = new_status
                        try:
                            obj = self.org_session.get(Attachment, UUID(att_id))
                        except Exception:
                            obj = None
                        if obj:
                            upd = await self.serialize_attachment(obj)
                            yield {"event": "update", "data": json.dumps([upd])}
                        else:
                            yield {"event": "update", "data": json.dumps([message])}
                        if all(st in self.TERMINAL_STATUSES for st in tracked.values()):
                            yield {"event": "complete", "data": json.dumps({"message": "all_attachments_completed", "final_statuses": tracked})}
                            return
        except Exception as e:
            logger.error("Attachment SSE stream error: %s", e, exc_info=True)
            yield {"event": "error", "data": json.dumps({"message": "internal_error", "reconnect": True})}
        finally:
            try:
                await self.redis_pubsub.close()
            except Exception:
                logger.debug("Redis pubsub close failed in finally (Attachment SSE)")
