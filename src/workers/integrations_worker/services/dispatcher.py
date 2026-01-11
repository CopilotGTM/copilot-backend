from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.observability.context import correlation_headers
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.crm.entities import (
    CompanySyncService,
    ContactSyncService,
    DealSyncService,
    DealTaskSyncService,
    OwnerSyncService,
    PipelineSyncService,
    UserSyncService,
)
from src.service.integrations.email_sync_service import EmailSyncService
from src.service.integrations.messaging_sync_service import MessagingSyncService

from ..adapters.calendar_adapter import upsert_calendar, upsert_calendar_incremental
from ..adapters.conversation_intelligence_adapter import sync_conversation_intelligence_data
from ..adapters.crm_adapter import sync_crm_data
from ..models.sync_models import SyncItemResult, SyncJobRequest, SyncJobResult
from ..providers.calendar.outlook_calendar import FetchEventsResult
from ..providers.messaging.teams import FetchChatsResult, FetchMessagesResult
from ..providers.registry import FetchFn, get_fetcher
from ..utils.dedupe import acquire_sync_lock, release_sync_lock

logger = get_logger(__name__)


async def _enrich_participants_after_crm_sync(
    organisation_id: UUID,
    integration_id: UUID,
) -> int:
    """
    Re-enrich deal participants after contacts/users sync.

    When contacts or users sync completes, we re-run participant enrichment
    to populate related_email fields that were empty during deals sync.

    Args:
        organisation_id: The organisation UUID
        integration_id: The CRM integration UUID

    Returns:
        Number of participants enriched
    """
    from src.api.dependencies.organisation_dependencies import acquire_org_async_session
    from src.service.integrations.crm.entities.deal_enrichment_service import (
        DealEnrichmentService,
    )

    async for session in acquire_org_async_session(organisation_id):
        enricher = DealEnrichmentService(db=session, organisation_id=organisation_id)
        enriched = await enricher.enrich_participants(integration_id)
        return enriched

    return 0


async def _enrich_activities_after_crm_sync(
    organisation_id: UUID,
    integration_id: UUID,
) -> None:
    """
    Re-enrich activities (tasks/tickets) after contacts/companies/deals sync.

    When contacts, companies, or deals sync completes, we re-run activity enrichment
    to populate FK references (contact_id, company_id, deal_id) that were stored
    only as external IDs during the initial activity sync.

    This enables bidirectional linking:
    - Activities synced first → store external IDs, resolve local IDs later
    - Related entities synced first → activities get enriched with local IDs

    Args:
        organisation_id: The organisation UUID
        integration_id: The CRM integration UUID
    """
    from src.api.dependencies.organisation_dependencies import acquire_org_async_session
    from src.service.integrations.crm.entities.task_enrichment_service import (
        TaskEnrichmentService,
    )

    async for session in acquire_org_async_session(organisation_id):
        enricher = TaskEnrichmentService(db=session, organisation_id=organisation_id)
        await enricher.enrich_tasks_for_integration(integration_id)
        await session.commit()
        return


async def _enrich_owners_after_users_sync(
    organisation_id: UUID,
    integration_id: UUID,
) -> dict[str, int]:
    """
    Re-enrich owner_id FK after users sync completes.

    In Salesforce (and HubSpot), the OwnerId field on any object points to a User record.
    When users sync, we need to resolve owner_external_id → owner_id (FK to crm_users)
    for entities that were synced before users were available.

    This updates:
    - crm_companies.owner_id
    - crm_contacts.owner_id
    - crm_deals.owner_id

    Args:
        organisation_id: The organisation UUID
        integration_id: The CRM integration UUID

    Returns:
        Dictionary with counts of enriched records per entity type
    """
    from src.api.dependencies.organisation_dependencies import acquire_org_async_session
    from src.service.integrations.crm.entities.owner_enrichment_service import (
        OwnerEnrichmentService,
    )

    async for session in acquire_org_async_session(organisation_id):
        enricher = OwnerEnrichmentService(db=session, organisation_id=organisation_id)
        results = await enricher.enrich_owners_for_integration(integration_id)
        await session.commit()
        return results

    return {"companies": 0, "contacts": 0, "deals": 0}


async def _sync_salesforce_users_alongside_crm(
    nango: NangoClient,
    integration_meta: IntegrationMetadata,
    provider_key: str,
    organisation_id: UUID,
    is_initial: bool,
) -> dict[str, Any]:
    """
    Sync Salesforce users alongside other CRM syncs.

    Since Nango doesn't have a built-in 'users' sync for Salesforce, we piggyback
    on other CRM syncs (opportunities, contacts, accounts, etc.) to also sync users.

    This ensures the crm_users table is populated with Salesforce User records,
    which allows:
    - Owner name/email resolution during activity enrichment
    - Deal owner information display
    - Participant matching for internal users

    Uses a Redis lock to prevent redundant syncs when multiple CRM webhooks
    arrive in quick succession (e.g., opportunities + contacts + accounts).

    Args:
        nango: Nango client instance
        integration_meta: Integration metadata with connection details
        provider_key: Nango provider config key (e.g., 'salesforce')
        organisation_id: Organisation UUID
        is_initial: Whether this is an initial (full) sync

    Returns:
        Dictionary with sync results
    """
    from src.workers.integrations_worker.providers.crm.salesforce import fetch_users

    # Use lock to prevent redundant users sync when multiple CRM syncs happen
    # Lock TTL of 300s (5 min) - users don't change often, so no need to sync more frequently
    integration_id_str = str(integration_meta.integration_id)
    lock_acquired = acquire_sync_lock(integration_id_str, "salesforce_users", ttl_seconds=300)
    if not lock_acquired:
        logger.debug(
            "Skipping Salesforce users sync - already synced recently: org=%s",
            organisation_id,
        )
        return {"users": 0, "status": "skipped", "reason": "recently_synced"}

    logger.info(
        "Syncing Salesforce users alongside CRM sync: org=%s connection=%s",
        organisation_id,
        integration_meta.connection_id,
    )

    try:
        # Fetch users from Salesforce via SOQL
        users = await fetch_users(
            nango=nango,
            connection_id=integration_meta.connection_id,
            is_initial=is_initial,
            last_synced_at=integration_meta.last_synced_at,
            data_type="users",
            provider_config_key=provider_key,
        )

        if not users:
            logger.debug("No users returned from Salesforce for org=%s", organisation_id)
            return {"users": 0, "status": "success", "reason": "no_records"}

        # Sync users to database
        async with _integration_session(organisation_id) as session:
            integration_row = await _reload_integration(session, integration_meta.integration_id)
            if not integration_row:
                logger.warning(
                    "Integration %s missing during Salesforce users sync",
                    integration_meta.integration_id,
                )
                return {"users": 0, "status": "failed", "reason": "integration_missing"}

            user_service = UserSyncService(db=session, organisation_id=organisation_id)
            metrics = await user_service.sync(
                integration=integration_row,
                records=users,
                provider="salesforce",
            )

            logger.info(
                "Salesforce users sync completed: org=%s users=%d",
                organisation_id,
                metrics.get("users", 0),
            )
            return metrics

    except Exception as e:
        logger.warning(
            "Failed to sync Salesforce users: org=%s error=%s",
            organisation_id,
            str(e),
            exc_info=True,
        )
        # Release lock on failure so next sync attempt can try again
        release_sync_lock(integration_id_str, "salesforce_users")
        return {"users": 0, "status": "failed", "error": str(e)}


async def _trigger_deal_tasks_on_initial_sync(
    organisation_id: UUID,
    integration_id: UUID | None,
    category: str,
    sync_type: str | None,
    sync_name: str | None,
    was_successful: bool,
) -> None:
    """
    Trigger deal worker tasks after a successful initial (full) sync.

    Deal tasks are triggered when:
    - CRM contacts/users sync completes → re-enriches participants, then triggers matching
    - Calendar events initial sync completes → triggers deal-meeting matching
    - Email initial sync completes → triggers deal-email matching

    **CRM Sync Order and Dependencies:**
    Nango sends separate webhooks for each sync type (deals, contacts, users, etc.).
    The deal matching algorithm needs contact emails, which come from:

    1. CRMDealParticipant.related_email - populated during enrichment
    2. Fallback: CRMContact lookup by external_id

    During 'deals' sync:
    - Creates crm_deals and crm_deal_participants
    - Participants have related_external_id but NO related_email (HubSpot associations
      only include contact IDs, not their properties)
    - enrich_participants() is called but contacts table may be empty → no emails filled

    During 'contacts' sync:
    - Creates crm_contacts with email addresses
    - NOW we can enrich participants with contact emails

    During 'users' sync:
    - Creates crm_users with email addresses
    - Allows enrichment of USER-type participants (deal owners, collaborators)

    Therefore, we trigger deal tasks after contacts/users sync, not deals sync.
    This ensures participant emails are available for matching.

    Args:
        organisation_id: The organisation UUID
        integration_id: The integration UUID (for re-enrichment)
        category: Integration category (calendar, email, crm)
        sync_type: Sync type from Nango (INITIAL, INCREMENTAL)
        sync_name: Data type synced (events, emails, contacts, deals, users, etc.)
        was_successful: Whether the sync completed successfully
    """
    # Only trigger on INITIAL sync (full sync)
    if sync_type != "INITIAL":
        return

    # Only trigger if sync was successful
    if not was_successful:
        return

    org_id_str = str(organisation_id)

    try:
        # Import here to avoid circular imports
        from src.workers.deal_worker.tasks.deal_email_tasks import process_deal_emails_task
        from src.workers.deal_worker.tasks.deal_tasks import process_deal_meetings_task

        # After contacts/users sync, re-enrich participants to fill in emails,
        # then trigger deal matching tasks
        if category == "crm" and sync_name in ("contacts", "users"):
            if integration_id:
                logger.info(
                    "Re-enriching deal participants after CRM %s sync: org=%s integration=%s",
                    sync_name,
                    organisation_id,
                    integration_id,
                )
                try:
                    enriched = await _enrich_participants_after_crm_sync(
                        organisation_id,
                        integration_id,
                    )
                    logger.info(
                        "Enriched %d participants after %s sync: org=%s",
                        enriched,
                        sync_name,
                        organisation_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to re-enrich participants: org=%s error=%s",
                        organisation_id,
                        str(e),
                    )

            # Trigger deal matching - now participants should have emails
            logger.info(
                "Triggering deal matching tasks after initial CRM %s sync: org=%s",
                sync_name,
                organisation_id,
            )
            process_deal_meetings_task.apply_async(args=[org_id_str], headers=correlation_headers())
            process_deal_emails_task.apply_async(args=[org_id_str], headers=correlation_headers())

        # Re-enrich activities after contacts/companies/deals/users sync.
        # Activities may have external IDs stored that couldn't be resolved
        # to local IDs during the initial activity sync.
        # - contacts/companies/deals: resolve contact_id, company_id, deal_id
        # - users: resolve owner details (owner_name, owner_email, owner_id)
        if (
            category == "crm"
            and sync_name in ("contacts", "companies", "deals", "users")
            and integration_id
        ):
            logger.info(
                "Re-enriching activities after CRM %s sync: org=%s integration=%s",
                sync_name,
                organisation_id,
                integration_id,
            )
            try:
                await _enrich_activities_after_crm_sync(
                    organisation_id,
                    integration_id,
                )
                logger.info(
                    "Enriched activities after %s sync: org=%s",
                    sync_name,
                    organisation_id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to re-enrich activities: org=%s error=%s",
                    organisation_id,
                    str(e),
                )

        # Re-enrich owner_id FK after users sync.
        # In Salesforce (and HubSpot), Users ARE Owners. When users sync completes,
        # resolve owner_external_id → owner_id for companies, contacts, and deals.
        if category == "crm" and sync_name == "users" and integration_id:
            logger.info(
                "Re-enriching owner_id after CRM users sync: org=%s integration=%s",
                organisation_id,
                integration_id,
            )
            try:
                results = await _enrich_owners_after_users_sync(
                    organisation_id,
                    integration_id,
                )
                logger.info(
                    "Enriched owner_id after users sync: org=%s results=%s",
                    organisation_id,
                    results,
                )
            except Exception as e:
                logger.warning(
                    "Failed to re-enrich owner_id: org=%s error=%s",
                    organisation_id,
                    str(e),
                )

        # Trigger deal-meeting matching after calendar events sync
        # This catches case where calendar is connected after CRM
        if category == "calendar" and sync_name == "events":
            logger.info(
                "Triggering deal-meeting matching after initial calendar sync: org=%s",
                organisation_id,
            )
            process_deal_meetings_task.apply_async(args=[org_id_str], headers=correlation_headers())

        # Trigger deal-email matching after email sync
        # This catches case where email is connected after CRM
        if category == "email" and sync_name in ("emails", "messages"):
            logger.info(
                "Triggering deal-email matching after initial email sync: org=%s",
                organisation_id,
            )
            process_deal_emails_task.apply_async(args=[org_id_str], headers=correlation_headers())

    except Exception as e:
        # Log but don't fail the sync - deal task triggering is best-effort
        logger.warning(
            "Failed to trigger deal tasks after initial sync: org=%s category=%s error=%s",
            organisation_id,
            category,
            str(e),
        )


async def _trigger_crm_notes_after_deals_sync(
    *,
    request: SyncJobRequest,
    was_successful: bool,
) -> None:
    """Trigger a CRM notes sync after deals/opportunities sync.

    This ensures deal/opportunity notes are fetched alongside the core deal sync,
    even if Nango is not configured to emit a separate notes webhook.
    """

    if not was_successful:
        return

    if request.category != "crm":
        return

    if request.sync_name not in {"deals", "opportunities"}:
        return

    try:
        from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_data_task

        sync_integration_data_task.apply_async(
            kwargs={
                "organisation_id": str(request.organisation_id),
                "integration_id": str(request.integration_id),
                "category": request.category,
                "provider": request.provider,
                "provider_config_key": request.provider_config_key,
                "sync_name": "notes",
                "sync_type": request.sync_type,
                "sync_variant": request.sync_variant,
                "incremental": request.incremental,
            },
            headers=correlation_headers(),
        )

        logger.info(
            "Triggered CRM notes sync after %s sync: org=%s integration=%s",
            request.sync_name,
            request.organisation_id,
            request.integration_id,
        )
    except Exception as exc:
        logger.warning(
            "Failed to trigger CRM notes sync: org=%s integration=%s error=%s",
            request.organisation_id,
            request.integration_id,
            str(exc),
        )


def _create_crm_service(
    data_type: str,
    session: AsyncSession,
    organisation_id: UUID,
) -> (
    DealSyncService
    | DealTaskSyncService
    | ContactSyncService
    | CompanySyncService
    | OwnerSyncService
    | UserSyncService
    | PipelineSyncService
):
    """Create appropriate CRM sync service based on data type."""
    service_map: dict[
        str,
        type[
            DealSyncService
            | DealTaskSyncService
            | ContactSyncService
            | CompanySyncService
            | OwnerSyncService
            | UserSyncService
            | PipelineSyncService
        ],
    ] = {
        "deals": DealSyncService,
        "opportunities": DealSyncService,  # Alias for Salesforce
        "deal_tasks": DealTaskSyncService,
        "tasks": DealTaskSyncService,  # Alias for backward compatibility
        "tickets": DealTaskSyncService,  # Salesforce Cases alias
        "notes": DealTaskSyncService,
        "contacts": ContactSyncService,
        "companies": CompanySyncService,
        "accounts": CompanySyncService,  # Alias for Salesforce (Account → companies)
        "owners": OwnerSyncService,
        "users": UserSyncService,
        "pipelines": PipelineSyncService,
    }

    service_class = service_map.get(data_type)
    if not service_class:
        raise ValueError(f"No service found for data_type: {data_type}")

    return service_class(db=session, organisation_id=organisation_id)


@dataclass(slots=True)
class IntegrationMetadata:
    integration_id: UUID
    connection_id: str
    integration_type: IntegrationType
    last_synced_at: datetime | None
    connection_metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class MonitoredChannelContext:
    """
    Metadata for monitored channels/chats.

    Supports both Team channels and chats (1:1, group, meeting).
    """

    channel_id: UUID
    external_channel_id: str  # Can be channel_id or chat_id
    channel_name: str | None
    channel_type: str | None  # public, private, direct_message, group_message, meeting
    # Team channel fields
    team_id: str | None
    team_name: str | None
    # Chat fields (chat_id is in external_channel_id)
    chat_type: str | None  # oneOnOne, group, meeting
    sync_from_date: datetime | None
    last_synced_at: datetime | None
    # Delta link for incremental message sync (per-channel delta)
    last_sync_cursor: str | None = None


@dataclass(slots=True)
class MonitoredMessagesFetchResult:
    messages: list[dict[str, Any]]
    monitored_channel_count: int
    # Delta links per channel for incremental sync (channel_id -> delta_link)
    channel_delta_links: dict[UUID, str | None] | None = None


@dataclass(slots=True)
class CategoryHandlerOutcome:
    item_result: SyncItemResult
    extra: dict[str, Any] | None = None
    should_stop: bool = False
    integration_meta: IntegrationMetadata | None = None


def _to_metadata(integration: KnowledgeIntegration) -> IntegrationMetadata:
    if not integration.connection_id:
        raise ValueError(f"Integration {integration.integration_id} has no connection_id")
    return IntegrationMetadata(
        integration_id=integration.integration_id,
        connection_id=integration.connection_id,
        integration_type=integration.integration_type,
        last_synced_at=integration.last_synced_at,
        connection_metadata=integration.connection_metadata,
    )


@asynccontextmanager
async def _integration_session(organisation_id: UUID) -> AsyncIterator[AsyncSession]:
    session_gen = acquire_org_async_session(organisation_id)
    session = await anext(session_gen)
    try:
        yield session
    finally:
        with suppress(StopAsyncIteration):
            await anext(session_gen)


async def _load_integration_metadata(
    organisation_id: UUID,
    integration_id: UUID,
) -> IntegrationMetadata:
    async with _integration_session(organisation_id) as session:
        stmt = select(KnowledgeIntegration).where(
            KnowledgeIntegration.integration_id == integration_id  # type: ignore[arg-type]
        )
        res = await session.execute(stmt)
        integration = res.scalars().first()
        if not integration:
            raise ValueError("Integration not found")
        return _to_metadata(integration)


async def _load_monitored_channel_contexts(
    *,
    organisation_id: UUID,
    integration_id: UUID,
) -> list[MonitoredChannelContext]:
    """Return lightweight metadata for channels currently marked as monitored."""
    async with _integration_session(organisation_id) as session:
        messaging_service = MessagingSyncService(session=session, organisation_id=organisation_id)
        channels = await messaging_service.get_monitored_channels(integration_id)

        contexts: list[MonitoredChannelContext] = []
        for channel in channels:
            metadata = dict(channel.channel_metadata or {})
            contexts.append(
                MonitoredChannelContext(
                    channel_id=channel.channel_id,
                    external_channel_id=channel.external_channel_id,
                    channel_name=channel.channel_name,
                    channel_type=channel.channel_type.value if channel.channel_type else None,
                    # Team channel fields
                    team_id=metadata.get("team_id"),
                    team_name=metadata.get("team_name"),
                    # Chat fields
                    chat_type=metadata.get("chat_type"),
                    sync_from_date=channel.sync_from_date,
                    last_synced_at=channel.last_synced_at,
                    # Delta link for incremental message sync
                    last_sync_cursor=channel.last_sync_cursor,
                )
            )

        return contexts


async def _reload_integration(
    session: AsyncSession,
    integration_id: UUID,
) -> KnowledgeIntegration | None:
    stmt = select(KnowledgeIntegration).where(KnowledgeIntegration.integration_id == integration_id)  # type: ignore[arg-type]
    res = await session.execute(stmt)
    return res.scalars().first()


_CI_ALIAS_MAP = {
    "call_speakers": "speakers",
    "speakers": "speakers",
    "transcript_segments": "segments",
    "call_transcript_segments": "segments",
    "segments": "segments",
    "call_analytics": "analytics",
    "analytics": "analytics",
}


async def _handle_calendar(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
    new_delta_link: str | None = None,
) -> CategoryHandlerOutcome:
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing calendar data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        # Route to incremental or full sync based on sync_type
        if sync_type and sync_type.upper() == "INCREMENTAL":
            count = await upsert_calendar_incremental(
                session=session,
                organisation_id=organisation_id,
                integration=integration_row,
                provider=provider,
                events=records,
            )
        else:
            count = await upsert_calendar(
                session=session,
                organisation_id=organisation_id,
                integration=integration_row,
                provider=provider,
                events=records,
            )

        # Save new deltaLink for Outlook calendar (enables true incremental sync)
        if new_delta_link and integration_row.integration_type == IntegrationType.OUTLOOK_CALENDAR:
            connection_metadata = dict(integration_row.connection_metadata or {})
            connection_metadata["outlook_delta_link"] = new_delta_link
            integration_row.connection_metadata = connection_metadata
            await (
                session.commit()
            )  # Commit to persist the deltaLink (async session doesn't auto-commit)
            logger.info(
                "Outlook calendar: saved new deltaLink for next sync (integration=%s)",
                integration_id,
            )

        logger.debug(
            "Calendar sync completed for data_type=%s: %d events processed (sync_type=%s)",
            data_type,
            count,
            sync_type,
        )
        await session.refresh(integration_row)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=count),
            integration_meta=_to_metadata(integration_row),
        )


async def _handle_email(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> CategoryHandlerOutcome:
    """
    Handle email folder and message sync.

    Supported data_types:
    - folders: Email folder/label sync
    - messages: Email message sync
    - emails: Alias for messages
    """
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing email data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        email_service = EmailSyncService(session=session, organisation_id=organisation_id)
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"

        # Route to appropriate sync method based on data_type and sync_type
        if data_type in ("folders",):
            if is_incremental:
                metrics = await email_service.sync_folders_incremental(
                    integration=integration_row,
                    folders=records,
                    provider=provider,
                )
            else:
                metrics = await email_service.sync_folders(
                    integration=integration_row,
                    folders=records,
                    provider=provider,
                )
        elif data_type in ("messages", "emails"):
            if is_incremental:
                metrics = await email_service.sync_messages_incremental(
                    integration=integration_row,
                    messages=records,
                    provider=provider,
                )
            else:
                metrics = await email_service.sync_messages(
                    integration=integration_row,
                    messages=records,
                    provider=provider,
                )
        else:
            logger.warning("Unknown email data_type: %s", data_type)
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="ignored", reason="unknown_data_type"),
                should_stop=True,
            )

        records_synced = metrics.get("records_processed", 0)

        logger.debug(
            "Email sync completed for data_type=%s (sync_type=%s): %s",
            data_type,
            sync_type,
            metrics,
        )
        await session.refresh(integration_row)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=records_synced),
            extra={"email": {data_type: metrics}},
            integration_meta=_to_metadata(integration_row),
        )


async def _handle_messaging(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
    sync_type: str | None = None,
    new_delta_link: str | None = None,  # noqa: ARG001 - Reserved for future use
) -> CategoryHandlerOutcome:
    """
    Handle messaging channel and message sync (Teams, Slack, etc.).

    Supported data_types:
    - channels: Messaging channel/team sync (pulls all available channels)
    - messages: Messaging message sync (syncs only monitored channels)

    Args:
        sync_type: Optional sync type ("INCREMENTAL" or "INITIAL")
    """
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing messaging data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        messaging_service = MessagingSyncService(session=session, organisation_id=organisation_id)

        # Route to appropriate sync method based on data_type and sync_type
        if data_type in ("channels", "chats"):
            # Both Team channels and chats use the same sync method
            # The service handles them differently based on channel_metadata.provider
            if sync_type and sync_type.upper() == "INCREMENTAL":
                metrics = await messaging_service.sync_channels_incremental(
                    integration=integration_row,
                    channels=records,
                    provider=provider,
                )
            else:
                metrics = await messaging_service.sync_channels(
                    integration=integration_row,
                    channels=records,
                    provider=provider,
                )
        elif data_type in ("messages", "chat_messages"):
            # For message sync, only process monitored channels/chats
            # Note: Messages don't need incremental sync - handled separately
            metrics = await messaging_service.sync_messages(
                integration=integration_row,
                messages=records,
                provider=provider,
            )
        else:
            logger.warning("Unknown messaging data_type: %s", data_type)
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="ignored", reason="unknown_data_type"),
                should_stop=True,
            )

        records_synced = metrics.get("records_processed", 0)

        # Note: Delta links for Teams chats listing are NOT supported in v1.0 API.
        # Delta IS supported for messages within a chat (/chats/{id}/messages/delta).

        logger.debug(
            "Messaging sync completed for data_type=%s: %s",
            data_type,
            metrics,
        )
        await session.refresh(integration_row)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=records_synced),
            extra={"messaging": {data_type: metrics}},
            integration_meta=_to_metadata(integration_row),
        )


async def _handle_crm(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
) -> CategoryHandlerOutcome:
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing CRM data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        crm_service = _create_crm_service(data_type, session, organisation_id)
        metrics = await sync_crm_data(
            service=crm_service,
            integration=integration_row,
            data_type=data_type,
            records=records,
            provider=provider,
            sync_type=sync_type,
        )
        records_synced = metrics.get("records_synced")
        if records_synced is None:
            records_synced = metrics.get("deals") or len(records)

        try:
            synced_count = int(records_synced or 0)
        except (TypeError, ValueError):
            synced_count = len(records)

        logger.debug(
            "CRM sync completed for data_type=%s: %s",
            data_type,
            metrics,
        )
        await session.refresh(integration_row)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=synced_count),
            extra={"crm": {data_type: metrics}},
            integration_meta=_to_metadata(integration_row),
        )


async def _sync_conversation_intelligence(
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
) -> CategoryHandlerOutcome:
    async with _integration_session(organisation_id) as session:
        integration_row = await _reload_integration(session, integration_id)
        if not integration_row:
            logger.warning(
                "Integration %s missing when processing CI data_type=%s",
                integration_id,
                data_type,
            )
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="failed", reason="integration_missing"),
                should_stop=True,
            )

        metrics = await sync_conversation_intelligence_data(
            session=session,
            integration=integration_row,
            data_type=data_type,
            records=records,
            provider=provider,
        )

        if metrics.get("status") == "ignored":
            reason = str(metrics.get("reason") or "ignored")
            return CategoryHandlerOutcome(
                item_result=SyncItemResult(status="ignored", reason=reason),
                should_stop=True,
            )

        normalized = data_type.lower()
        metric_key = _CI_ALIAS_MAP.get(normalized, normalized)
        records_synced = metrics.get(metric_key)
        if records_synced is None:
            records_synced = metrics.get("records_processed", 0)

        try:
            synced_count = int(records_synced or 0)
        except (TypeError, ValueError):
            synced_count = 0

        logger.debug(
            "Conversation intelligence sync completed for data_type=%s: %s",
            data_type,
            metrics,
        )

        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="success", records_synced=synced_count),
            extra={"conversation_intelligence": {data_type: metrics}},
        )


async def _dispatch_category(
    category: str,
    organisation_id: UUID,
    integration_id: UUID,
    data_type: str,
    records: list[dict[str, Any]],
    provider: str,
    *,
    sync_type: str | None = None,
    new_delta_link: str | None = None,
) -> CategoryHandlerOutcome:
    handler_map = {
        "calendar": _handle_calendar,
        "email": _handle_email,
        "messaging": _handle_messaging,
        "crm": _handle_crm,
        "conversation_intelligence": _sync_conversation_intelligence,
    }

    handler = handler_map.get(category)
    if not handler:
        logger.warning("Unsupported category: %s", category)
        return CategoryHandlerOutcome(
            item_result=SyncItemResult(status="ignored", reason="unsupported_category"),
            should_stop=True,
        )

    # CRM, calendar, and email handlers accept sync_type for incremental optimization
    if category == "crm":
        return await _handle_crm(
            organisation_id=organisation_id,
            integration_id=integration_id,
            data_type=data_type,
            records=records,
            provider=provider,
            sync_type=sync_type,
        )

    if category == "calendar":
        return await _handle_calendar(
            organisation_id=organisation_id,
            integration_id=integration_id,
            data_type=data_type,
            records=records,
            provider=provider,
            sync_type=sync_type,
            new_delta_link=new_delta_link,
        )

    if category == "email":
        return await _handle_email(
            organisation_id=organisation_id,
            integration_id=integration_id,
            data_type=data_type,
            records=records,
            provider=provider,
            sync_type=sync_type,
        )

    if category == "messaging":
        return await _handle_messaging(
            organisation_id=organisation_id,
            integration_id=integration_id,
            data_type=data_type,
            records=records,
            provider=provider,
            sync_type=sync_type,
            new_delta_link=new_delta_link,
        )

    return await handler(
        organisation_id=organisation_id,
        integration_id=integration_id,
        data_type=data_type,
        records=records,
        provider=provider,
    )


@contextmanager
def _sync_lock_guard(integration_id: str, data_type: str) -> Iterator[bool]:
    lock_acquired = acquire_sync_lock(integration_id, data_type)
    try:
        yield lock_acquired
    finally:
        if lock_acquired:
            release_sync_lock(integration_id, data_type)


def _apply_outcome(
    result: SyncJobResult,
    outcome: CategoryHandlerOutcome,
) -> tuple[IntegrationMetadata | None, bool]:
    if outcome.extra:
        for key, value in outcome.extra.items():
            if isinstance(value, dict):
                result.extra.setdefault(key, {}).update(value)
            else:
                result.extra[key] = value
    return outcome.integration_meta, outcome.should_stop


async def _fetch_monitored_messages(
    *,
    fetcher: FetchFn,
    nango: NangoClient,
    integration_meta: IntegrationMetadata,
    provider_key: str,
    organisation_id: UUID,
    integration_id: UUID,
    sync_data_type: str,
    is_initial: bool,
) -> MonitoredMessagesFetchResult:
    """Collect Teams messages only for channels flagged as monitored.

    Supports delta query for incremental message sync:
    - First sync: fetches all messages and stores delta_link
    - Subsequent syncs: uses stored delta_link to fetch only new/changed messages
    - Expired delta links (410 Gone): automatically falls back to full sync
    """
    channel_contexts = await _load_monitored_channel_contexts(
        organisation_id=organisation_id,
        integration_id=integration_id,
    )

    if not channel_contexts:
        logger.info(
            "No monitored messaging channels found for integration=%s; skipping fetch.",
            integration_id,
        )
        return MonitoredMessagesFetchResult(messages=[], monitored_channel_count=0)

    aggregated_messages: list[dict[str, Any]] = []
    # Collect new delta links per channel for storage after sync
    channel_delta_links: dict[UUID, str | None] = {}

    for channel in channel_contexts:
        # Determine if this is a Team channel or a chat
        is_chat = channel.chat_type is not None  # Has chat_type = it's a chat
        is_team_channel = channel.team_id is not None  # Has team_id = it's a Team channel

        # Skip if neither (shouldn't happen)
        if not is_chat and not is_team_channel:
            logger.warning(
                "Skipping monitored channel without team or chat context: integration=%s channel_id=%s",
                integration_id,
                channel.external_channel_id,
            )
            continue

        # Determine if this channel needs initial sync
        # If we have a stored delta_link, use it for incremental sync
        has_delta_link = channel.last_sync_cursor is not None
        channel_is_initial = is_initial or (channel.last_synced_at is None and not has_delta_link)

        last_synced_at = channel.last_synced_at or integration_meta.last_synced_at
        if last_synced_at is None:
            last_synced_at = channel.sync_from_date

        try:
            # Build fetch parameters based on type
            fetch_params: dict[str, Any] = {
                "nango": nango,
                "connection_id": integration_meta.connection_id,
                "is_initial": channel_is_initial,
                "last_synced_at": last_synced_at,
                "data_type": sync_data_type,
                "provider_config_key": provider_key,
            }

            # Add type-specific parameters
            if is_team_channel:
                # Team channel: needs team_id and channel_id
                fetch_params["team_id"] = channel.team_id
                fetch_params["channel_id"] = channel.external_channel_id
            else:
                # Chat: needs chat_id
                fetch_params["chat_id"] = channel.external_channel_id

            # Pass stored delta_link for incremental sync (if available)
            if has_delta_link and not channel_is_initial:
                fetch_params["delta_link"] = channel.last_sync_cursor
                logger.debug(
                    "Using stored delta_link for incremental sync: channel=%s",
                    channel.external_channel_id,
                )

            fetch_result = await fetcher(**fetch_params)

            # Handle FetchMessagesResult with delta_link
            if isinstance(fetch_result, FetchMessagesResult):
                messages = fetch_result.messages
                new_delta_link = fetch_result.delta_link
                if new_delta_link:
                    channel_delta_links[channel.channel_id] = new_delta_link
                    logger.debug(
                        "Received new delta_link for channel=%s",
                        channel.external_channel_id,
                    )
            else:
                # Fallback for fetchers that don't return FetchMessagesResult
                messages = fetch_result

        except Exception as e:
            # Check for expired delta link (410 Gone) - retry with fresh sync
            error_str = str(e).lower()
            if "410" in error_str or "gone" in error_str or "expired" in error_str:
                logger.warning(
                    "Delta link expired for channel=%s, retrying with fresh sync",
                    channel.external_channel_id,
                )
                try:
                    # Retry without delta_link (fresh sync)
                    fetch_params.pop("delta_link", None)
                    fetch_params["is_initial"] = True

                    fetch_result = await fetcher(**fetch_params)

                    if isinstance(fetch_result, FetchMessagesResult):
                        messages = fetch_result.messages
                        new_delta_link = fetch_result.delta_link
                        if new_delta_link:
                            channel_delta_links[channel.channel_id] = new_delta_link
                    else:
                        messages = fetch_result
                except Exception:
                    logger.exception(
                        "Failed to fetch messages after delta_link expiry: integration=%s channel=%s",
                        integration_id,
                        channel.external_channel_id,
                    )
                    continue
            else:
                logger.exception(
                    "Failed to fetch messages: integration=%s type=%s id=%s",
                    integration_id,
                    "chat" if is_chat else "channel",
                    channel.external_channel_id,
                )
                continue

        if not messages:
            continue

        # Enrich messages with context
        for message in messages:
            if is_team_channel:
                message.setdefault("teamId", channel.team_id)
                message.setdefault("channelId", channel.external_channel_id)
            else:
                message.setdefault("chatId", channel.external_channel_id)
            message["_copilot_channel_internal_id"] = str(channel.channel_id)

        aggregated_messages.extend(messages)

    return MonitoredMessagesFetchResult(
        messages=aggregated_messages,
        monitored_channel_count=len(channel_contexts),
        channel_delta_links=channel_delta_links if channel_delta_links else None,
    )


async def _save_channel_delta_links(
    *,
    organisation_id: UUID,
    channel_delta_links: dict[UUID, str | None],
) -> int:
    """
    Save new delta links to messaging_channels.last_sync_cursor after successful message sync.

    This enables true incremental sync for subsequent syncs - we only fetch
    messages changed since the last delta.

    Args:
        organisation_id: The organization UUID
        channel_delta_links: Map of channel_id -> new_delta_link

    Returns:
        Number of channels updated
    """
    if not channel_delta_links:
        return 0

    from datetime import UTC, datetime

    from sqlalchemy import update
    from src.domain.models.organisation_tables.sync import MessagingChannel

    async with _integration_session(organisation_id) as session:
        updated_count = 0
        for channel_id, delta_link in channel_delta_links.items():
            if delta_link:
                stmt = (
                    update(MessagingChannel)
                    .where(MessagingChannel.channel_id == channel_id)
                    .values(
                        last_sync_cursor=delta_link,
                        last_synced_at=datetime.now(UTC),
                    )
                )
                result = await session.execute(stmt)
                if result.rowcount > 0:
                    updated_count += 1
                    logger.debug(
                        "Saved delta_link for channel %s",
                        channel_id,
                    )

        await session.commit()
        logger.info(
            "Saved delta links for %d channels (org=%s)",
            updated_count,
            organisation_id,
        )
        return updated_count


async def _execute_category_sync(
    *,
    request: SyncJobRequest,
    nango: NangoClient,
    integration_meta: IntegrationMetadata,
    provider_key: str,
    result: SyncJobResult,
    organisation_id: UUID,
    integration_id: UUID,
    sync_data_type: str,
) -> tuple[IntegrationMetadata | None, bool]:
    fetcher = get_fetcher(request.category, request.provider, sync_data_type)
    logger.debug("Fetcher obtained: %s", fetcher.__name__ if fetcher else "None")
    if not fetcher:
        logger.warning(
            "No fetcher found for category=%s provider=%s data_type=%s",
            request.category,
            request.provider,
            sync_data_type,
        )
        result.items[sync_data_type] = SyncItemResult(status="ignored", reason="no_fetcher")
        return None, True

    logger.debug("Starting Fetch for Connection ID: %s", integration_meta.connection_id)
    logger.debug("Last Synced At: %s", integration_meta.last_synced_at)
    logger.debug("Is Initial Sync: %s", not request.incremental)

    # Track Outlook calendar deltaLink for true incremental sync
    is_outlook_calendar = (
        request.category == "calendar"
        and integration_meta.integration_type == IntegrationType.OUTLOOK_CALENDAR
    )
    # Track Teams chats deltaLink for true incremental sync
    is_teams_chats = (
        request.category == "messaging"
        and sync_data_type == "chats"
        and integration_meta.integration_type == IntegrationType.TEAMS
    )
    new_delta_link: str | None = None

    monitored_fetch: MonitoredMessagesFetchResult | None = None
    if request.category == "messaging" and sync_data_type == "messages":
        monitored_fetch = await _fetch_monitored_messages(
            fetcher=fetcher,
            nango=nango,
            integration_meta=integration_meta,
            provider_key=provider_key,
            organisation_id=organisation_id,
            integration_id=integration_id,
            sync_data_type=sync_data_type,
            is_initial=not request.incremental,
        )
        records = monitored_fetch.messages
        logger.debug(
            "Fetched %d messages across %d monitored channels",
            len(records),
            monitored_fetch.monitored_channel_count,
        )
    else:
        # Build base fetch parameters
        fetch_kwargs: dict[str, Any] = {
            "nango": nango,
            "connection_id": integration_meta.connection_id,
            "is_initial": not request.incremental,
            "last_synced_at": integration_meta.last_synced_at,
            "data_type": sync_data_type,
            "provider_config_key": provider_key,
        }

        # For entities with incremental sync support, pass sync context
        # These parameters enable the fetchers to add _sync_metadata to records
        # CRM fetchers accept sync_type and response_results parameters
        # for adding _sync_metadata to records. Non-CRM fetchers don't.
        incremental_sync_entities = {
            # CRM entities only - these fetchers accept sync_type/response_results
            "deals",
            "opportunities",  # Deals
            "contacts",
            "leads",  # Contacts
            "companies",
            "accounts",  # Companies
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",  # Deal Tasks
            "owners",  # Owners
            "users",  # Users
            "pipelines",  # Pipelines
        }
        # All categories that support incremental sync at the handler/service level
        # (email, calendar, messaging handle incremental at service layer, not fetcher layer)
        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        if (
            request.category in incremental_sync_categories
            and sync_data_type in incremental_sync_entities
        ):
            fetch_kwargs["sync_type"] = request.sync_type
            fetch_kwargs["response_results"] = request.response_results
            logger.debug(
                "Passing sync context to %s fetcher: sync_type=%s response_results=%s",
                sync_data_type,
                request.sync_type,
                request.response_results,
            )

        # For Outlook calendar incremental sync, pass stored deltaLink for true incremental
        # This enables fetching only changed events instead of all events
        if is_outlook_calendar and request.incremental:
            stored_delta_link = (integration_meta.connection_metadata or {}).get(
                "outlook_delta_link"
            )
            if stored_delta_link:
                fetch_kwargs["delta_link"] = stored_delta_link
                logger.info("Outlook calendar: using stored deltaLink for true incremental sync")
            else:
                logger.info("Outlook calendar: no stored deltaLink, will establish baseline")

        # Note: Teams chats listing (/me/chats) does NOT support delta query in v1.0 API.
        # Delta is only available in beta API. We fetch all chats each time.
        # Delta IS supported for messages within a chat (/chats/{id}/messages/delta).
        if is_teams_chats:
            logger.info("Teams chats: fetching all chats (delta not supported in v1.0 API)")

        fetch_result = await fetcher(**fetch_kwargs)

        # Handle Outlook calendar's FetchEventsResult return type
        if is_outlook_calendar and isinstance(fetch_result, FetchEventsResult):
            records = fetch_result.events
            new_delta_link = fetch_result.delta_link
            if new_delta_link:
                logger.info("Outlook calendar: received new deltaLink from API")
        # Handle Teams chats' FetchChatsResult return type
        # Note: delta_link is always None since delta is not supported for /me/chats in v1.0
        elif is_teams_chats and isinstance(fetch_result, FetchChatsResult):
            records = fetch_result.chats
            # delta_link not supported for chats listing in v1.0 API
        else:
            records = fetch_result

        logger.debug("Fetched %d records for data_type=%s", len(records), sync_data_type)
    if not records:
        logger.debug(
            "No records returned for data_type=%s; skipping processing.",
            sync_data_type,
        )
        reason = "no_records"
        if monitored_fetch and monitored_fetch.monitored_channel_count == 0:
            reason = "no_monitored_channels"
        result.items[sync_data_type] = SyncItemResult(
            status="success", records_synced=0, reason=reason
        )
        return None, True

    logger.debug("Sample record for data_type=%s: %s", sync_data_type, records[0])

    outcome = await _dispatch_category(
        category=request.category,
        organisation_id=organisation_id,
        integration_id=integration_id,
        data_type=sync_data_type,
        records=records,
        provider=request.provider,
        sync_type=request.sync_type,
        new_delta_link=new_delta_link if (is_outlook_calendar or is_teams_chats) else None,
    )

    result.items[sync_data_type] = outcome.item_result

    # Save per-channel delta links for messaging messages (enables incremental sync)
    if (
        request.category == "messaging"
        and sync_data_type == "messages"
        and monitored_fetch
        and monitored_fetch.channel_delta_links
        and outcome.item_result.status == "success"
    ):
        try:
            delta_count = await _save_channel_delta_links(
                organisation_id=organisation_id,
                channel_delta_links=monitored_fetch.channel_delta_links,
            )
            logger.info(
                "Saved delta links for %d channels after message sync: org=%s",
                delta_count,
                organisation_id,
            )
        except Exception as e:
            # Don't fail the main sync if delta link save fails
            logger.warning(
                "Failed to save channel delta links: org=%s error=%s",
                organisation_id,
                str(e),
            )

    # For Salesforce CRM syncs, also sync users alongside
    # This ensures crm_users table is populated even without a dedicated Nango 'users' sync
    if (
        request.category == "crm"
        and integration_meta.integration_type == IntegrationType.SALESFORCE
        and outcome.item_result.status == "success"
        and sync_data_type != "users"  # Avoid infinite loop if users sync is triggered
    ):
        try:
            users_result = await _sync_salesforce_users_alongside_crm(
                nango=nango,
                integration_meta=outcome.integration_meta or integration_meta,
                provider_key=provider_key,
                organisation_id=organisation_id,
                is_initial=not request.incremental,
            )
            # Add users sync result to the overall result
            users_synced = users_result.get("users", 0)
            result.items["users"] = SyncItemResult(
                status=users_result.get("status", "success"),
                records_synced=users_synced,
                reason=users_result.get("reason"),
            )
            logger.info(
                "Salesforce users sync completed alongside %s: org=%s users=%d",
                sync_data_type,
                organisation_id,
                users_synced,
            )
        except Exception as e:
            logger.warning(
                "Failed to sync Salesforce users alongside %s: org=%s error=%s",
                sync_data_type,
                organisation_id,
                str(e),
            )
            # Don't fail the main sync if users sync fails
            result.items["users"] = SyncItemResult(
                status="failed",
                reason="users_sync_error",
            )

    return _apply_outcome(result, outcome)


async def execute_sync(request: SyncJobRequest) -> dict[str, Any]:
    """
    Execute a sync job for a specific integration.

    Args:
        request: Sync job request containing organisation, integration, and data type info

    Returns:
        Dictionary with sync results including status and items synced
    """

    # Initialize Required client
    nango = NangoClient()

    # Initialize variables
    organisation_id: UUID = UUID(str(request.organisation_id))
    integration_id: UUID = UUID(str(request.integration_id))

    # Validate required fields
    if not request.sync_name:
        return {
            "status": "failed",
            "error": "sync_name is required",
            "items": {},
        }

    sync_data_type: str = request.sync_name  # Type narrowing

    try:
        integration_meta = await _load_integration_metadata(organisation_id, integration_id)
        logger.info(
            "Integration found: connection_id=%s last_synced=%s",
            integration_meta.connection_id,
            integration_meta.last_synced_at,
        )

        # Prepare for sync
        result = SyncJobResult(status="success")

        # Determine provider key
        provider_key = request.provider_config_key or to_nango_connect_integration_key(
            integration_meta.integration_type
        )
        logger.info("Using provider config key: %s", provider_key)
        logger.info("Processing data type: %s", sync_data_type)

        # Check sync lock
        with _sync_lock_guard(str(request.integration_id), sync_data_type) as lock_acquired:
            if not lock_acquired:
                result.items[sync_data_type] = SyncItemResult(status="skipped", reason="locked")
                return result.to_dict()

            try:
                integration_update, should_stop = await _execute_category_sync(
                    request=request,
                    nango=nango,
                    integration_meta=integration_meta,
                    provider_key=provider_key,
                    result=result,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    sync_data_type=sync_data_type,
                )
                integration_meta = integration_update or integration_meta
                if should_stop:
                    return result.to_dict()

            except Exception as e:
                logger.error(
                    "Error syncing data_type=%s: %s",
                    sync_data_type,
                    str(e),
                    exc_info=True,
                )
                result.items[sync_data_type] = SyncItemResult(
                    status="failed",
                    reason="sync_error",
                    error=str(e),
                )

        # Determine overall status
        if any(item.status == "failed" for item in result.items.values()):
            result.status = "failed"
        elif any(
            item.status not in {"success", "ignored", "skipped"} for item in result.items.values()
        ):
            result.status = "partial"
        elif result.items and all(item.status == "success" for item in result.items.values()):
            result.status = "success"

        # Log summary
        success_count = sum(1 for item in result.items.values() if item.status == "success")
        total_records = sum(
            item.records_synced or 0 for item in result.items.values() if item.status == "success"
        )
        logger.info(
            "Sync job completed: org=%s integration=%s success=%d/%d total_records=%d",
            request.organisation_id,
            request.integration_id,
            success_count,
            1,
            total_records,
        )

        # Trigger deal tasks on initial sync completion
        await _trigger_deal_tasks_on_initial_sync(
            organisation_id=organisation_id,
            integration_id=integration_id,
            category=request.category,
            sync_type=request.sync_type,
            sync_name=request.sync_name,
            was_successful=(result.status == "success"),
        )

        # Trigger CRM notes sync after deals/opportunities sync (best-effort)
        await _trigger_crm_notes_after_deals_sync(
            request=request,
            was_successful=(result.status == "success"),
        )

        return result.to_dict()

    except ValueError as not_found:
        logger.warning(
            "Integration %s not found for org %s: %s",
            integration_id,
            organisation_id,
            str(not_found),
        )
        return SyncJobResult(
            status="failed",
            extra={"error": "integration_not_found", "integration_id": str(integration_id)},
        ).to_dict()
    except Exception as e:
        logger.error(
            "Sync job failed: org=%s integration=%s error=%s",
            request.organisation_id,
            request.integration_id,
            str(e),
            exc_info=True,
        )
        return SyncJobResult(
            status="failed", extra={"error": str(e), "error_type": type(e).__name__}
        ).to_dict()
    finally:
        with suppress(Exception):
            await nango.close()
