"""Knowledge source SSE streaming service.

Provides Server-Sent Events streaming for knowledge source status updates
using Redis pub/sub for real-time notifications. Handles connection lifecycle,
filtering, timeouts, and error recovery.
"""
import asyncio
import json
from typing import Dict, Set, List, Optional, AsyncGenerator
from uuid import UUID

from sqlalchemy import select, not_
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.messaging.redis_pubsub import AsyncRedisPubSub
from src.infra.messaging.redis_channels import channel_for_knowledge_update_status
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate

logger = get_logger("knowledge_sse_service")


def _get_status_str(status_enum):
    """Safely get the string value of a status enum."""
    if status_enum is None:
        return None
    return getattr(status_enum, "value", str(status_enum))


class KnowledgeSSEError(Exception):
    """Domain exception carrying an HTTP status code for API translation."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)

class KnowledgeSourceSSEService:
    """Service for streaming knowledge source status via SSE with Redis pub/sub.

    Notes:
        * Must receive an organisation-scoped session (search_path already set).
        * Historically terminal status set stored *string* values; switched to enum
          objects for type‑safety, while still comparing via their `.value` against
          DB values which are persisted as enum values (strings).
    """

    TERMINAL_STATUSES = {
        OrganisationSourceStatus.COMPLETED,
        OrganisationSourceStatus.FAILED,
    }
    # Cached list of terminal *values* (strings) for query / comparisons.
    TERMINAL_STATUS_VALUES = [s.value for s in TERMINAL_STATUSES]
    
    def __init__(self, org_session: AsyncSession, organisation_id: UUID, user_id: UUID):
        """Initialize SSE service.

        Args:
            org_session: Organisation-scoped SQLModel Session (search_path set)
            organisation_id: Current organisation UUID
            user_id: Current user UUID (used to scope sources)
        """
        # Store context
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id

        # Redis pub/sub client
        self.redis_pubsub = AsyncRedisPubSub()

        # Azure blob storage client used for generating read SAS URLs (download + thumbnails)
        self.storage = AzureOrganisationBlobStorage(self.organisation_id)

    # Backwards compatibility with older code paths that expected `.session`
    @property
    def session(self):  # pragma: no cover - trivial delegation
        return self.org_session

    async def close(self):  # parity with upload service dependency cleanup
        try:
            await self.redis_pubsub.close()
            # Close storage client (aiohttp) to avoid resource warnings
            try:
                await self.storage.close()
            except Exception:
                logger.debug("Storage close failed (SSE)", exc_info=True)
        except Exception:  # swallow
            logger.debug("Redis pubsub close failed (SSE)", exc_info=True)
    
    async def get_initial_sources(self, explicit_ids: Optional[Set[UUID]] = None) -> List[OrganisationSource]:
        """Fetch initial source snapshot based on filter criteria (async)."""
        base_filters = [
            OrganisationSource.organisation_id == self.organisation_id,
            OrganisationSource.user_id == self.user_id,
            OrganisationSource.is_deleted == False,  # exclude soft-deleted
        ]

        stmt = select(OrganisationSource).where(*base_filters)

        if explicit_ids:
            stmt = stmt.where(OrganisationSource.source_id.in_(explicit_ids))
            res = await self.org_session.execute(stmt)
            return res.scalars().all()

        logger.info("Fetching initial sources for User non-terminal IDs")
        stmt = stmt.where(not_(OrganisationSource.status.in_(self.TERMINAL_STATUS_VALUES)))
        res = await self.org_session.execute(stmt)
        sources = res.scalars().all()
        logger.debug("Initial non-terminal sources count=%d", len(sources))
        return sources
    
    async def serialize_source(self, source: OrganisationSource) -> Dict:
        """Convert OrganisationSource to dict including dynamic SAS URLs.

        Adds:
            download_url: SAS URL to download original file (if storage_path set)
            thumbnail_small_url / thumbnail_medium_url: SAS URLs if thumbnails exist

        SAS generation failures are swallowed (fields omitted) to avoid breaking stream.
        """

        # Build a validated model instance and then convert to dict so callers
        # receive a predictable, typed payload.
        model = KnowledgeSourceEventUpdate(
            source_id=str(source.source_id),
            status=_get_status_str(source.status),
            updated_at=source.updated_at.isoformat() if source.updated_at else None,
            error_message=source.error_message,
            progress=None,
        )
        data = model.model_dump()

        if model.status in [OrganisationSourceStatus.EXTRACTING, OrganisationSourceStatus.COMPLETED]:
            # Generate SAS URLs conditionally (only if paths exist)
            if getattr(source, "thumbnail_small_path", None):
                try:
                    data["thumbnail_small_url"] = await self.storage.generate_read_sas(
                        source.thumbnail_small_path, minutes=120
                    )
                except Exception:
                    logger.debug("Thumbnail small SAS generation failed", exc_info=True)
            if getattr(source, "thumbnail_medium_path", None):
                try:
                    data["thumbnail_medium_url"] = await self.storage.generate_read_sas(
                        source.thumbnail_medium_path, minutes=120
                    )
                except Exception:
                    logger.debug("Thumbnail medium SAS generation failed", exc_info=True)
        return data
    
    def _send_event(self, event_type: str, data) -> Dict:
        """Helper to format SSE event as dict."""
        return {"event": event_type, "data": json.dumps(data)}

    async def _handle_initial_sources(
        self, 
        sources: List[OrganisationSource], 
        watched_status: Dict[str, str]
    ) -> AsyncGenerator[Dict, None]:
        """Serialize initial sources and yield initial status events."""
        serialized_list = await asyncio.gather(*(self.serialize_source(s) for s in sources))
        for s in serialized_list:
            watched_status[s["source_id"]] = s["status"]
        yield self._send_event("status", serialized_list)
        
        if self._all_sources_terminal(watched_status):
            yield self._send_event("complete", {"message": "all_sources_terminal"})

    def _handle_message(
        self, 
        message: Dict, 
        allowed_ids_set: Optional[Set[str]], 
        watched_status: Dict[str, str]
    ) -> Optional[Dict]:
        """Process a single Redis pub/sub message and return an event dict if applicable."""
        source_id = message.get("source_id")
        if not source_id or (allowed_ids_set and source_id not in allowed_ids_set):
            return None
        watched_status[source_id] = message.get("status")
        return self._send_event("status", [message])

    def _all_sources_terminal(self, watched_status: Dict[str, str]) -> bool:
        """Check if all watched sources are in terminal status."""
        return all(status in self.TERMINAL_STATUS_VALUES for status in watched_status.values())

    def _should_timeout(self, start_time: float, current_time: float, timeout_seconds: Optional[int]) -> bool:
        """Check if stream should timeout."""
        return timeout_seconds is not None and (current_time - start_time) > timeout_seconds

    def _should_send_heartbeat(self, current_time: float, last_heartbeat: float, heartbeat_interval: float) -> bool:
        """Check if heartbeat should be sent."""
        return (current_time - last_heartbeat) >= heartbeat_interval

    async def stream_events(
        self,
        explicit_ids: Optional[Set[UUID]] = None,
        timeout_seconds: Optional[int] = 1800,
        heartbeat_interval: float = 25.0
    ) -> AsyncGenerator[Dict, None]:
        """
        Generate SSE events for knowledge source status updates.
        """
        logger.info(f"Starting SSE stream for org {self.organisation_id}, user {self.user_id}")
        start_time = asyncio.get_event_loop().time()
        last_heartbeat = start_time
        watched_status: Dict[str, str] = {}
        allowed_ids_set = {str(sid) for sid in explicit_ids} if explicit_ids else None

        try:
            # Handle initial sources
            initial_sources = await self.get_initial_sources(explicit_ids)
            if not initial_sources:
                # No initial sources: release DB session before returning
                try:
                    await self.org_session.close()
                except Exception:
                    logger.debug("SSE org_session early-close failed (no sources)")
                yield self._send_event("complete", {"message": "no_sources_to_watch"})
                return

            async for event in self._handle_initial_sources(initial_sources, watched_status):
                yield event
                if event["event"] == "complete":
                    # All sources already terminal: release DB session before returning
                    try:
                        await self.org_session.close()
                    except Exception:
                        logger.debug("SSE org_session early-close failed (complete)")
                    return

            # Release DB session before long-lived Redis loop to avoid holding pool connections
            try:
                await self.org_session.close()
            except Exception:
                logger.debug("SSE org_session early-close failed (post-initial)")

            # Stream updates from Redis
            async for event in self._stream_redis_updates(
                start_time, last_heartbeat, watched_status, allowed_ids_set, 
                timeout_seconds, heartbeat_interval
            ):
                yield event

        except Exception as e:
            logger.error(f"SSE stream error for org {self.organisation_id}: {e}", exc_info=True)
            yield self._send_event("error", {
                "message": "Internal stream error. Please reconnect.",
                "error_type": type(e).__name__,
                "reconnect": True
            })
        finally:
            try:
                await self.redis_pubsub.close()
            except Exception:
                logger.debug("Redis pubsub close failed in finally (SSE)")

    async def _stream_redis_updates(
        self,
        start_time: float,
        last_heartbeat: float,
        watched_status: Dict[str, str],
        allowed_ids_set: Optional[Set[str]],
        timeout_seconds: Optional[int],
        heartbeat_interval: float
    ) -> AsyncGenerator[Dict, None]:
        """Stream updates from Redis pub/sub."""
        channel = channel_for_knowledge_update_status(self.organisation_id)
        
        async for message in self.redis_pubsub.subscribe_to_channel(channel):
            current_time = asyncio.get_event_loop().time()

            # Check timeout
            if self._should_timeout(start_time, current_time, timeout_seconds):
                yield self._send_event("timeout", {
                    "message": "Stream timeout reached. Please reconnect.",
                    "timeout_seconds": timeout_seconds,
                    "reconnect": True
                })
                return

            # Send heartbeat if needed
            if self._should_send_heartbeat(current_time, last_heartbeat, heartbeat_interval):
                last_heartbeat = current_time
                yield self._send_event("ping", {"timestamp": int(current_time)})

            # Handle status message
            status_event = self._handle_message(message, allowed_ids_set, watched_status)
            if status_event:
                yield status_event

            # Check if all sources are complete
            if self._all_sources_terminal(watched_status):
                yield self._send_event("complete", {"message": "all_sources_completed"})
                return
