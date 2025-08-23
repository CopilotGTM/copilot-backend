"""Knowledge source SSE streaming service.

Provides Server-Sent Events streaming for knowledge source status updates
using Redis pub/sub for real-time notifications. Handles connection lifecycle,
filtering, timeouts, and error recovery.
"""
import asyncio
import json
from typing import Dict, Set, List, Optional, AsyncGenerator
from uuid import UUID

from sqlmodel import select
from sqlmodel import Session

from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.infra.messaging.redis_pubsub import AsyncRedisPubSub, channel_for_knowledge_update_status
from src.infra.utils.logger import get_logger
# (Upload service imported previously for historical reasons; not required here.)

logger = get_logger("knowledge_sse_service")

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
    
    def __init__(
        self, org_session: Session, organisation_id: UUID, user_id: UUID
    ):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.redis_pubsub = AsyncRedisPubSub()

    # Backwards compatibility with older code paths that expected `.session`
    @property
    def session(self):  # pragma: no cover - trivial delegation
        return self.org_session

    async def close(self):  # parity with upload service dependency cleanup
        try:
            await self.redis_pubsub.close()
        except Exception:  # swallow
            logger.debug("Redis pubsub close failed (SSE)", exc_info=True)
    
    async def get_initial_sources(self, explicit_ids: Optional[Set[UUID]] = None) -> List[OrganisationSource]:
        """Fetch initial source snapshot based on filter criteria.
        
        Args:
            explicit_ids: If provided, fetch only these specific source IDs.
                         If None, fetch all non-terminal sources for the user.
        
        Returns:
            List of OrganisationSource objects
        """
        # (Search path now guaranteed by router-managed session lifetime.)
        # Always constrain by organisation + user to avoid cross‑tenant leakage
        stmt = select(OrganisationSource).where(
            OrganisationSource.organisation_id == self.organisation_id,
            OrganisationSource.user_id == self.user_id,
        )
        
        if explicit_ids:
            # Explicit mode: get requested IDs regardless of status
            logger.info("Fetching initial sources for User sent IDs")
            stmt = stmt.where(OrganisationSource.source_id.in_(explicit_ids))
            sources = self.org_session.exec(stmt).all()
            return sources

        # Filter out terminal statuses (compare with underlying stored values)
        logger.info("Fetching initial sources for User non-terminal IDs")
        stmt = stmt.where(OrganisationSource.status.not_in(self.TERMINAL_STATUS_VALUES))
        sources = self.org_session.exec(stmt).all()
        logger.debug("Initial non-terminal sources count=%d", len(sources))
        return sources
    
    def serialize_source(self, source: OrganisationSource) -> Dict:
        """Convert OrganisationSource to dict for JSON serialization.
        
        Args:
            source: OrganisationSource instance
            
        Returns:
            Serialized source data
        """
        status_val = source.status.value if hasattr(source.status, 'value') else str(source.status)
        return {
            "source_id": str(source.source_id),
            "status": status_val,
            "filename": source.filename,
            "mimetype": source.mimetype,
            "size_bytes": source.size_bytes,
            "updated_at": source.updated_at.isoformat() if source.updated_at else None,
            "created_at": source.created_at.isoformat() if source.created_at else None,
            "error_message": source.error_message,
            "storage_path": source.storage_path,
        }
    
    async def stream_events(
        self,
        explicit_ids: Optional[Set[UUID]] = None,
        timeout_seconds: Optional[int] = 1800,  # 30 minutes default
        heartbeat_interval: float = 25.0
    ) -> AsyncGenerator[Dict, None]:
        """Generate SSE events for knowledge source status updates.
        
        Args:
            explicit_ids: Optional set of source IDs to monitor
            timeout_seconds: Stream timeout (None for no timeout)
            heartbeat_interval: Seconds between heartbeat pings
            
        Yields:
            SSE event dictionaries with 'event' and 'data' keys
        """
        logger.info(f"Starting SSE stream for org {self.organisation_id}, user {self.user_id}")
        start_time = asyncio.get_event_loop().time()
        last_heartbeat = start_time
        
        try:
            # 1. Get initial snapshot
            initial_sources = await self.get_initial_sources(explicit_ids)
            watched_status: Dict[str, str] = {}
            
            # Early completion if nothing to watch
            if not initial_sources:
                yield {
                    "event": "complete",
                    "data": json.dumps({"message": "no_sources_to_watch"})
                }
                return

            for source in initial_sources:
                serialized = self.serialize_source(source)
                watched_status[serialized["source_id"]] = serialized["status"]
            
            # Emit initial snapshot
            yield {
                "event": "snapshot",
                "data": json.dumps([self.serialize_source(s) for s in initial_sources])
            }

            # Check if all are already terminal
            if all(status in self.TERMINAL_STATUS_VALUES for status in watched_status.values()):
                yield {
                    "event": "complete", 
                    "data": json.dumps({"message": "all_sources_terminal"})
                }
                return
            
            # 2. Subscribe to Redis channel for updates
            channel = channel_for_knowledge_update_status(self.organisation_id)
            
            async for message in self.redis_pubsub.subscribe_to_channel(channel):
                current_time = asyncio.get_event_loop().time()
                
                # Check timeout
                if timeout_seconds and (current_time - start_time) > timeout_seconds:
                    yield {
                        "event": "timeout",
                        "data": json.dumps({
                            "message": "Stream timeout reached. Please reconnect.",
                            "timeout_seconds": timeout_seconds,
                            "reconnect": True
                        })
                    }
                    return
                
                # Send heartbeat if needed
                if (current_time - last_heartbeat) >= heartbeat_interval:
                    last_heartbeat = current_time
                    yield {
                        "event": "ping",
                        "data": json.dumps({"timestamp": int(current_time)})
                    }
                
                # Process Redis message
                source_id = message.get("source_id")
                if not source_id:
                    continue
                
                # Filter by explicit IDs if provided
                if explicit_ids and source_id not in {str(sid) for sid in explicit_ids}:
                    continue
                
                # Update tracked status
                new_status = message.get("status")
                if source_id in watched_status:
                    old_status = watched_status[source_id]
                    if old_status != new_status:
                        watched_status[source_id] = new_status
                        
                        # Emit update event
                        yield {
                            "event": "update",
                            "data": json.dumps([message])
                        }
                        
                        # Check if all sources are now terminal
                        if all(status in self.TERMINAL_STATUS_VALUES for status in watched_status.values()):
                            yield {
                                "event": "complete",
                                "data": json.dumps({
                                    "message": "all_sources_completed",
                                    "final_statuses": watched_status
                                })
                            }
                            return
                
        except Exception as e:
            logger.error(f"SSE stream error for org {self.organisation_id}: {e}", exc_info=True)
            yield {
                "event": "error",
                "data": json.dumps({
                    "message": "Internal stream error. Please reconnect.",
                    "error_type": type(e).__name__,
                    "reconnect": True
                })
            }
        finally:
            # Ensure pubsub closed if close() not explicitly called
            try:
                await self.redis_pubsub.close()
            except Exception:
                logger.debug("Redis pubsub close failed in finally (SSE)")