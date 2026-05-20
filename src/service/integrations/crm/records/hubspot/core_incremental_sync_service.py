"""Core HubSpot CRM incremental repair service.

This service discovers changed object ids via HubSpot Search and then refreshes
each object through the existing full-object fetch + normalizer/writer paths.
It intentionally does not schedule itself; the integrations worker calls it from
the per-integration Tier 3 task after the all-org fan-out has selected the row.

The important correctness rule is cursor ordering: provider cursors are written
only after all requested object families finish their refresh work. A retry can
therefore replay the lookback window and let stale-snapshot checks absorb
duplicates instead of silently dropping a HubSpot update.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.incremental.object_refresher import (
    HubSpotIncrementalObjectRefresher,
)
from src.service.integrations.crm.records.hubspot.incremental.registry import (
    HUBSPOT_INCREMENTAL_OBJECT_KEYS,
    build_hubspot_incremental_refresher_registry,
)
from src.service.integrations.crm.records.hubspot.incremental_cursor_metadata import (
    get_hubspot_incremental_search_start,
    is_hubspot_incremental_ready,
    set_hubspot_incremental_cursor,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo
from src.service.integrations.sync_history_manager import datetime_now_naive
from src.workers.integrations_worker.providers.crm.hubspot.incremental.changed_objects_search_fetcher import (
    fetch_changed_hubspot_objects,
)

logger = get_logger(__name__)

_SUPPORTED_OBJECTS = frozenset(HUBSPOT_INCREMENTAL_OBJECT_KEYS)


@dataclass(slots=True, frozen=True)
class HubSpotIncrementalObjectResult:
    """Summary for one object family in a HubSpot incremental run.

    `cursor_source_modified_at` is the highest HubSpot modified timestamp seen
    for this object family during the run. It may be `None` when Search returns
    no rows, which intentionally preserves the prior provider cursor.
    """

    object_key: str
    seen_count: int
    written_count: int
    skipped_stale_count: int
    cursor_source_modified_at: datetime | None
    updated_metadata: dict[str, Any]


@dataclass(slots=True, frozen=True)
class HubSpotCoreIncrementalSyncResult:
    """Summary for one integration-level core incremental run.

    Worker tasks use the aggregate properties for Celery result payloads and
    logs; detailed per-object cursor state remains in `updated_metadata`.
    """

    object_results: tuple[HubSpotIncrementalObjectResult, ...]
    updated_metadata: dict[str, Any]

    @property
    def seen_count(self) -> int:
        """Aggregate number of changed rows observed across all object types."""
        return sum(result.seen_count for result in self.object_results)

    @property
    def written_count(self) -> int:
        """Aggregate number of rows written across all object types."""
        return sum(result.written_count for result in self.object_results)

    @property
    def skipped_stale_count(self) -> int:
        """Aggregate number of stale rows skipped across all object types."""
        return sum(result.skipped_stale_count for result in self.object_results)


class HubSpotCoreIncrementalSyncService:
    """Run incremental HubSpot CRM repair for one org-scoped integration.

    Called by the integrations worker after initial sync has created a starting
    cursor. The service keeps provider I/O outside DB sessions where possible:
    it loads the token/metadata, releases the session, fetches HubSpot changes,
    writes refreshed objects through the existing writers, then stores cursors.
    """

    async def run(
        self,
        *,
        organisation_id,
        integration_id,
        object_keys: tuple[str, ...] = HUBSPOT_INCREMENTAL_OBJECT_KEYS,
        cursor_updated_at: datetime | None = None,
    ) -> HubSpotCoreIncrementalSyncResult:
        """Load integration context, process requested objects, and persist metadata.

        Cursor metadata is saved once after all requested object families finish.
        If a worker crashes mid-run, the previous cursor remains in place and
        the next retry replays the lookback window instead of silently skipping
        provider changes.
        """
        # Load org-scoped integration metadata before provider calls, then close
        # the session so HubSpot latency does not pin a tenant DB connection.
        token_info, metadata = await self._load_token_context(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        updated_metadata = dict(metadata)
        if not is_hubspot_incremental_ready(updated_metadata, object_keys):
            # The all-org fan-out also checks readiness, but a child task may
            # already be queued while the initial sync metadata is still being
            # written. Skipping here prevents a partial cursor document from
            # causing noisy failures or premature provider polling.
            logger.info(
                "HubSpot incremental sync skipped because initial sync is not ready",
                extra={
                    "integration_id": str(integration_id),
                    "organisation_id": str(organisation_id),
                    "object_keys": object_keys,
                },
            )
            return HubSpotCoreIncrementalSyncResult(
                object_results=(),
                updated_metadata=updated_metadata,
            )

        object_results: list[HubSpotIncrementalObjectResult] = []
        # Build one registry for the run so each object family follows the same
        # Search -> full fetch -> writer contract without this orchestrator
        # knowing the shape of deals, contacts, tasks, or notes.
        refresher_registry = build_hubspot_incremental_refresher_registry()
        now = cursor_updated_at or datetime.now(UTC)

        for object_key in object_keys:
            # Feed each object family's updated cursor into the next one. This
            # preserves independent per-object state inside one shared metadata
            # document without losing changes written by earlier families.
            result = await self.sync_object(
                object_key=object_key,
                token_info=token_info,
                metadata=updated_metadata,
                refresher_registry=refresher_registry,
                cursor_updated_at=now,
            )
            updated_metadata = result.updated_metadata
            object_results.append(result)

        # Save the shared metadata document after all object families complete.
        # If the worker dies before this point, every family replays from the
        # previous cursor on retry; that is noisy but prevents data loss.
        await self._save_metadata(
            organisation_id=organisation_id,
            integration_id=integration_id,
            metadata=updated_metadata,
        )
        return HubSpotCoreIncrementalSyncResult(
            object_results=tuple(object_results),
            updated_metadata=updated_metadata,
        )

    async def sync_object(
        self,
        *,
        object_key: str,
        token_info: IntegrationTokenInfo,
        metadata: dict[str, Any],
        refresher_registry: dict[str, HubSpotIncrementalObjectRefresher] | None = None,
        cursor_updated_at: datetime | None = None,
    ) -> HubSpotIncrementalObjectResult:
        """Incrementally refresh one supported HubSpot CRM object family.

        HubSpot Search is used only to discover changed ids. Each id is then
        fetched as a full object snapshot and passed through the normal writer
        so webhooks, incremental polling, and full sync converge on the same
        persistence rules.
        """
        if object_key not in _SUPPORTED_OBJECTS:
            raise ValueError(f"Unsupported HubSpot incremental object: {object_key}")
        # Tests can inject a tiny registry, but production uses the central
        # registry so object coverage stays aligned with the worker object list.
        refresher_registry = refresher_registry or build_hubspot_incremental_refresher_registry()
        refresher = refresher_registry[object_key]

        # The search starts before the stored cursor by a small lookback because
        # provider timestamps and webhook delivery are not perfectly ordered.
        # Writers own stale-row detection, so replaying a small overlap is safer
        # than advancing past a late-arriving update.
        search_start = get_hubspot_incremental_search_start(
            metadata,
            object_key,
        )
        if search_start is None:
            raise ValueError(f"No HubSpot incremental start cursor for {object_key}")

        # This provider call happens outside an org DB session. The follow-up
        # per-object writers acquire short-lived sessions only when they commit
        # local state.
        changed = await fetch_changed_hubspot_objects(
            token_info=token_info,
            object_type=object_key,
            modified_since=search_start,
        )

        written_count = 0
        skipped_stale_count = 0
        max_source_modified_at: datetime | None = None

        for changed_object in changed:
            # Advance the cursor only to the greatest provider-modified value
            # actually observed in the search response. Empty searches preserve
            # the old cursor so a temporary HubSpot/query issue cannot move the
            # watermark to wall-clock time and hide missed records.
            if changed_object.source_modified_at is not None and (
                max_source_modified_at is None
                or changed_object.source_modified_at > max_source_modified_at
            ):
                max_source_modified_at = changed_object.source_modified_at

            refresh_result = await refresher.refresh(
                token_info=token_info,
                changed_object=changed_object,
            )
            # Refreshers return writer counters instead of rows so this service
            # can aggregate outcomes without depending on object-specific write
            # result types.
            written_count += refresh_result.written_count
            skipped_stale_count += refresh_result.skipped_stale_count

        # Metadata stores both the provider watermark and operational counters.
        # The cursor helper preserves the previous watermark when Search is
        # empty, so a quiet poll records health without advancing past unseen
        # HubSpot rows.
        updated_metadata = set_hubspot_incremental_cursor(
            metadata,
            object_key,
            cursor_source_modified_at=max_source_modified_at,
            cursor_updated_at=cursor_updated_at or datetime.now(UTC),
            seen_count=len(changed),
            written_count=written_count,
            skipped_stale_count=skipped_stale_count,
        )

        logger.info(
            "HubSpot incremental object sync complete",
            extra={
                "integration_id": str(token_info.integration_id),
                "organisation_id": str(token_info.organisation_id),
                "object_key": object_key,
                "seen_count": len(changed),
                "written_count": written_count,
                "skipped_stale_count": skipped_stale_count,
            },
        )
        return HubSpotIncrementalObjectResult(
            object_key=object_key,
            seen_count=len(changed),
            written_count=written_count,
            skipped_stale_count=skipped_stale_count,
            cursor_source_modified_at=max_source_modified_at,
            updated_metadata=updated_metadata,
        )

    async def _load_token_context(self, *, organisation_id, integration_id):
        """Load token and cursor metadata for one tenant-scoped HubSpot integration."""
        async for session in acquire_org_async_session(organisation_id):
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
            )
            # The organisation filter is the trust boundary. The integration id
            # may come from a queued task payload, so never resolve it globally.
            integration = (await session.execute(statement)).scalar_one_or_none()
            if integration is None:
                raise ValueError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            integration_type = integration.integration_type
            if isinstance(integration_type, str):
                integration_type = IntegrationType(integration_type)
            if integration_type != IntegrationType.HUBSPOT:
                raise ValueError(
                    f"Integration {integration_id} is {integration_type.value}, expected hubspot"
                )
            if not integration.is_active:
                raise ValueError(f"Integration {integration_id} is not active")
            # Return detached primitives only. The SQLAlchemy row must not leak
            # past this session because provider calls and refresh writes happen
            # after the org session is released.
            return (
                IntegrationTokenInfo.from_integration(integration, organisation_id),
                dict(integration.connection_metadata or {}),
            )
        raise ValueError("Could not acquire org session for HubSpot incremental sync")

    async def _save_metadata(self, *, organisation_id, integration_id, metadata: dict[str, Any]):
        """Persist the final cursor metadata after a successful incremental run."""
        async for session in acquire_org_async_session(organisation_id):
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
            )
            # Re-resolve the integration under the same org boundary before
            # writing cursors; queued retries must not update a row from another
            # tenant even if ids are accidentally mixed.
            integration = (await session.execute(statement)).scalar_one_or_none()
            if integration is None:
                # The integration may have been disconnected while the worker
                # was polling HubSpot. Dropping the cursor write is safer than
                # recreating or updating a no-longer-visible tenant row.
                return
            integration.connection_metadata = metadata
            integration.last_synced_at = datetime_now_naive()
            session.add(integration)
            await session.commit()
            return


__all__ = [
    "HubSpotCoreIncrementalSyncResult",
    "HubSpotCoreIncrementalSyncService",
    "HubSpotIncrementalObjectResult",
]
