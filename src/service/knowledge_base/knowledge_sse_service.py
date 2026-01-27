"""Knowledge source SSE streaming service.

Provides Server-Sent Events streaming for knowledge source status updates
using Redis pub/sub for real-time notifications. Handles connection lifecycle,
filtering, timeouts, and error recovery.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import cast
from uuid import UUID

from sqlalchemy import not_, select
from sqlmodel import col
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.database.sessions import org_async_session_scope
from src.infra.messaging.redis_channels import channel_for_knowledge_update_status
from src.infra.messaging.redis_pubsub import AsyncRedisPubSub
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger

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
        * Acquires short-lived organisation-scoped sessions internally as needed.
        * Historically terminal status set stored *string* values; switched to enum
          objects for type‑safety, while still comparing via their `.value` against
          DB values which are persisted as enum values (strings).
    """

    TERMINAL_STATUSES = {
        OrganisationSourceStatus.COMPLETED,
        OrganisationSourceStatus.FAILED,
        OrganisationSourceStatus.DELETING,  # Deletion in progress is also terminal for SSE
    }
    # Cached list of terminal *values* (strings) for query / comparisons.
    TERMINAL_STATUS_VALUES = [s.value for s in TERMINAL_STATUSES]

    # Additional statuses that indicate complete removal (source no longer exists)
    DELETION_COMPLETE_STATUSES = {"deleted"}

    def __init__(self, organisation_id: UUID, user_id: UUID):
        """Initialize SSE service.

        Args:
            organisation_id: Current organisation UUID
            user_id: Current user UUID (used to scope sources)
        """
        # Store context
        self.organisation_id = organisation_id
        self.user_id = user_id

        # Redis pub/sub client
        self.redis_pubsub = AsyncRedisPubSub()

        # Azure blob storage client used for generating read SAS URLs (download + thumbnails)
        self.storage = AzureOrganisationBlobStorage(self.organisation_id)

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

    async def get_initial_sources(
        self, explicit_ids: set[UUID] | None = None
    ) -> list[OrganisationSource]:
        """Fetch initial source snapshot based on filter criteria (async)."""
        base_filters = [
            col(OrganisationSource.organisation_id) == self.organisation_id,
            col(OrganisationSource.user_id) == self.user_id,
            col(OrganisationSource.is_deleted).is_(False),
        ]

        stmt = select(OrganisationSource).where(*base_filters)

        if explicit_ids:
            stmt = stmt.where(col(OrganisationSource.source_id).in_(explicit_ids))
            async with org_async_session_scope(self.organisation_id) as session:
                res = await session.execute(stmt)
                return cast(list[OrganisationSource], list(res.scalars().all()))

        logger.info("Fetching initial sources for User non-terminal IDs")
        stmt = stmt.where(not_(col(OrganisationSource.status).in_(self.TERMINAL_STATUS_VALUES)))
        async with org_async_session_scope(self.organisation_id) as session:
            res = await session.execute(stmt)
            sources = cast(list[OrganisationSource], list(res.scalars().all()))
        logger.debug("Initial non-terminal sources count=%d", len(sources))
        return sources

    async def serialize_source(self, source: OrganisationSource) -> dict:
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
            status=_get_status_str(source.status) or "",
            updated_at=source.updated_at.isoformat() if source.updated_at else None,
            error_message=source.error_message,
            progress=None,
        )
        data = model.model_dump()

        if model.status in [
            OrganisationSourceStatus.EXTRACTING,
            OrganisationSourceStatus.COMPLETED,
        ]:
            # Generate SAS URLs conditionally (only if paths exist)
            if source.thumbnail_small_path:
                try:
                    data["thumbnail_small_url"] = await self.storage.generate_read_sas(
                        cast(str, source.thumbnail_small_path), minutes=120
                    )
                except Exception:
                    logger.debug("Thumbnail small SAS generation failed", exc_info=True)
            if source.thumbnail_medium_path:
                try:
                    data["thumbnail_medium_url"] = await self.storage.generate_read_sas(
                        cast(str, source.thumbnail_medium_path), minutes=120
                    )
                except Exception:
                    logger.debug("Thumbnail medium SAS generation failed", exc_info=True)
        return data

    def _send_event(self, event_type: str, data) -> dict:
        """Helper to format SSE event as dict."""
        return {"event": event_type, "data": json.dumps(data)}

    async def _handle_initial_sources(
        self, sources: list[OrganisationSource], watched_status: dict[str, str]
    ) -> AsyncGenerator[dict, None]:
        """Serialize initial sources and yield initial status events."""
        serialized_list = await asyncio.gather(*(self.serialize_source(s) for s in sources))
        for s in serialized_list:
            watched_status[s["source_id"]] = s["status"]
        yield self._send_event("status", serialized_list)

        if self._all_sources_terminal(watched_status):
            yield self._send_event("complete", {"message": "all_sources_terminal"})

    def _handle_message(
        self,
        message: dict,
        allowed_ids_set: set[str] | None,
        watched_status: dict[str, str],
    ) -> dict | None:
        """Process a single Redis pub/sub message and return an event dict if applicable.

        Handles deletion status updates specially:
        - 'deleting' status with progress updates
        - 'deleted' status indicates source removal (emit special event type)
        """
        source_id = message.get("source_id")
        if not source_id or (allowed_ids_set and source_id not in allowed_ids_set):
            return None

        status = str(message.get("status", ""))
        watched_status[source_id] = status

        # Use 'deleted' event type for completed deletions to signal UI removal
        if status in self.DELETION_COMPLETE_STATUSES:
            return self._send_event("deleted", [message])

        return self._send_event("status", [message])

    def _all_sources_terminal(self, watched_status: dict[str, str]) -> bool:
        """Check if all watched sources are in terminal status or deleted."""
        all_terminal_values = set(self.TERMINAL_STATUS_VALUES) | self.DELETION_COMPLETE_STATUSES
        return all(status in all_terminal_values for status in watched_status.values())

    def _should_timeout(
        self, start_time: float, current_time: float, timeout_seconds: int | None
    ) -> bool:
        """Check if stream should timeout."""
        return timeout_seconds is not None and (current_time - start_time) > timeout_seconds

    def _should_send_heartbeat(
        self, current_time: float, last_heartbeat: float, heartbeat_interval: float
    ) -> bool:
        """Check if heartbeat should be sent."""
        return (current_time - last_heartbeat) >= heartbeat_interval

    async def stream_events(
        self,
        explicit_ids: set[UUID] | None = None,
        timeout_seconds: int | None = 1800,
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[dict, None]:
        """
        Generate SSE events for knowledge source status updates.
        """
        logger.info(f"Starting SSE stream for org {self.organisation_id}, user {self.user_id}")
        start_time = asyncio.get_event_loop().time()
        last_heartbeat = start_time
        watched_status: dict[str, str] = {}
        allowed_ids_set = {str(sid) for sid in explicit_ids} if explicit_ids else None

        try:
            # Handle initial sources
            initial_sources = await self.get_initial_sources(explicit_ids)
            if not initial_sources:
                yield self._send_event("complete", {"message": "no_sources_to_watch"})
                return

            async for event in self._handle_initial_sources(initial_sources, watched_status):
                yield event
                if event["event"] == "complete":
                    return

            # Stream updates from Redis
            async for event in self._stream_redis_updates(
                start_time,
                last_heartbeat,
                watched_status,
                allowed_ids_set,
                timeout_seconds,
                heartbeat_interval,
            ):
                yield event

        except Exception as e:
            logger.error(f"SSE stream error for org {self.organisation_id}: {e}", exc_info=True)
            yield self._send_event(
                "error",
                {
                    "message": "Internal stream error. Please reconnect.",
                    "error_type": type(e).__name__,
                    "reconnect": True,
                },
            )
        finally:
            try:
                await self.redis_pubsub.close()
            except Exception:
                logger.debug("Redis pubsub close failed in finally (SSE)")

    async def _stream_redis_updates(
        self,
        start_time: float,
        last_heartbeat: float,
        watched_status: dict[str, str],
        allowed_ids_set: set[str] | None,
        timeout_seconds: int | None,
        heartbeat_interval: float,
    ) -> AsyncGenerator[dict, None]:
        """Stream updates from Redis pub/sub."""
        channel = channel_for_knowledge_update_status(self.organisation_id)

        async for message in self.redis_pubsub.subscribe_to_channel(channel):
            current_time = asyncio.get_event_loop().time()

            # Check timeout
            if self._should_timeout(start_time, current_time, timeout_seconds):
                yield self._send_event(
                    "timeout",
                    {
                        "message": "Stream timeout reached. Please reconnect.",
                        "timeout_seconds": timeout_seconds,
                        "reconnect": True,
                    },
                )
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
