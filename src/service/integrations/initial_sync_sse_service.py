"""Replayable SSE delivery for durable initial-sync progress snapshots."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationSyncHistory,
    KnowledgeIntegration,
    SyncStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.redis_channels import channel_for_initial_sync
from src.infra.messaging.redis_streams import AsyncRedisStreams
from src.infra.utils.logger import get_logger
from src.service.integrations.initial_sync_progress_service import (
    INITIAL_SYNC_KIND,
    InitialSyncProgressService,
)

logger = get_logger(__name__)
_TERMINAL_STATUSES = {SyncStatus.SUCCESS, SyncStatus.PARTIAL, SyncStatus.FAILED}


class InitialSyncSSEService:
    """Authorize and stream one user's initial-sync run within one tenant."""

    def __init__(self, organisation_id: UUID, sync_id: UUID, user_id: UUID) -> None:
        self.organisation_id = organisation_id
        self.sync_id = sync_id
        self.user_id = user_id
        self.stream_key = channel_for_initial_sync(organisation_id, sync_id)
        self._streams: AsyncRedisStreams | None = None

    async def ensure_user_access(self) -> bool | None:
        """Return access only when the tenant-scoped, user-owned run exists."""
        return True if await self._get_record() is not None else None

    async def get_snapshot(self) -> dict | None:
        """Load the latest durable snapshot for initial page rendering."""
        record = await self._get_record()
        return InitialSyncProgressService.snapshot(record) if record else None

    async def close(self) -> None:
        """Close the lazy Redis reader without masking the stream outcome."""
        if self._streams is not None:
            try:
                await self._streams.close()
            except Exception:
                logger.debug("Initial sync Redis close failed", exc_info=True)
            finally:
                self._streams = None

    async def stream_events(
        self,
        *,
        resume_from_id: str | None = None,
        timeout_seconds: int = 300,
        heartbeat_interval: float = 25.0,
    ) -> AsyncGenerator[dict, None]:
        """Yield durable state first, then replay Redis events from the cursor."""
        record = await self._get_record()
        if record is None:
            return

        durable = InitialSyncProgressService.snapshot(record)
        yield {"event": "ready", "data": json.dumps({"sync_id": str(self.sync_id)})}
        yield {"event": "snapshot", "data": json.dumps(durable)}
        if record.status in _TERMINAL_STATUSES:
            yield {"event": "complete", "data": json.dumps({"sync_id": str(self.sync_id)})}
            return

        self._streams = AsyncRedisStreams()
        loop = asyncio.get_running_loop()
        started = loop.time()
        last_ping = started
        next_id = resume_from_id or "0-0"
        block_ms = max(1, int(heartbeat_interval * 500))
        try:
            while True:
                now = loop.time()
                if timeout_seconds and now - started >= timeout_seconds:
                    yield {
                        "event": "timeout",
                        "data": json.dumps({"sync_id": str(self.sync_id)}),
                    }
                    return
                if now - last_ping >= heartbeat_interval:
                    last_ping = now
                    yield {"event": "ping", "data": json.dumps({"t": int(now)})}

                entries = await self._streams.xread(
                    self.stream_key, next_id, block_ms=block_ms, count=50
                )
                for entry_id, frame in entries:
                    next_id = entry_id
                    if frame.get("event") != "snapshot":
                        continue
                    snapshot = frame.get("data", {})
                    yield {
                        "id": entry_id,
                        "event": "snapshot",
                        "data": json.dumps(snapshot),
                    }
                    if snapshot.get("status") in {status.value for status in _TERMINAL_STATUSES}:
                        yield {
                            "event": "complete",
                            "data": json.dumps({"sync_id": str(self.sync_id)}),
                        }
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Initial sync stream read failed: sync_id=%s", self.sync_id, exc_info=True
            )
            yield {
                "event": "error",
                "data": json.dumps(
                    {"message": "Initial sync stream unavailable", "reconnect": True}
                ),
            }
        finally:
            await self.close()

    async def _get_record(self) -> IntegrationSyncHistory | None:
        async for session in acquire_org_async_session(self.organisation_id):
            statement = (
                select(IntegrationSyncHistory)
                .join(
                    KnowledgeIntegration,
                    col(KnowledgeIntegration.integration_id)
                    == col(IntegrationSyncHistory.integration_id),
                )
                .where(
                    col(IntegrationSyncHistory.organisation_id) == self.organisation_id,
                    col(IntegrationSyncHistory.sync_id) == self.sync_id,
                    col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                    col(KnowledgeIntegration.created_by_user_id) == self.user_id,
                )
            )
            record = (await session.execute(statement)).scalar_one_or_none()
            if record is None or record.sync_metadata.get("kind") != INITIAL_SYNC_KIND:
                return None
            return record
        return None


__all__ = ["InitialSyncSSEService"]
