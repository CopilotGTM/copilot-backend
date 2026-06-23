"""Salesforce CDC subscriber task."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import (
    CRMNote,
    CRMSourceObjectType,
    CRMTask,
)
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.database.sessions import (
    acquire_org_async_session,
)
from src.infra.utils.async_runner import run_async
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.salesforce.companies.company_sync_service import (
    SalesforceCompanyRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_sync_service import (
    SalesforceContactRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.deals.deal_sync_service import (
    SalesforceDealRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.notes.note_sync_service import (
    SalesforceNoteRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.tasks.task_sync_service import (
    SalesforceTaskRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.users.user_sync_service import (
    SalesforceUserRecordSyncService,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    get_access_token,
)
from src.service.integrations.sync_history_manager import SyncHistoryManager
from src.service.integrations.webhooks.salesforce.decoder import decode_salesforce_change_event
from src.service.integrations.webhooks.salesforce.metadata import (
    ACCOUNT_CDC_CHANNEL,
    CONTACT_CDC_CHANNEL,
    CONTENT_DOCUMENT_CDC_CHANNEL,
    CONTENT_DOCUMENT_LINK_CDC_CHANNEL,
    OPPORTUNITY_CDC_CHANNEL,
    TASK_CDC_CHANNEL,
    USER_CDC_CHANNEL,
    SalesforceCdcMetadataError,
    get_salesforce_cdc_metadata_for_channel,
    get_salesforce_opportunity_cdc_metadata,
)
from src.service.integrations.webhooks.salesforce.replay import decode_replay_id, encode_replay_id
from src.service.integrations.webhooks.salesforce.subscription_state import (
    SalesforceCdcSubscriptionStateService,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.client import send_integrations_task_async
from src.workers.integrations_worker.config import TASK_LIMITS
from src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher import (
    SalesforceAccountsFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher import (
    SalesforceContactsFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher import (
    SalesforceNotesFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_client import (
    SalesforcePubSubClient,
    SalesforcePubSubKeepalive,
    SalesforcePubSubReplayIdError,
)
from src.workers.integrations_worker.providers.crm.salesforce.single_opportunity_fetcher import (
    SalesforceSingleOpportunityFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.tasks_fetcher import (
    SalesforceTasksFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.users_fetcher import (
    SalesforceUsersFetcher,
)

_TASK_NAME = "integrations.run_salesforce_cdc_subscriber"
_L = TASK_LIMITS[_TASK_NAME]
_LEASE_SECONDS = 300
_MAX_RUNTIME_SECONDS = 3300
_FAILURE_REQUEUE_DELAY_SECONDS = 60
_DELETE_CHANGE_TYPES = {"DELETE"}
_REPAIR_TASK_BY_CHANNEL = {
    OPPORTUNITY_CDC_CHANNEL: "integrations.sync_salesforce_deals",
    ACCOUNT_CDC_CHANNEL: "integrations.sync_salesforce_companies",
    CONTACT_CDC_CHANNEL: "integrations.sync_salesforce_contacts",
    USER_CDC_CHANNEL: "integrations.sync_salesforce_users",
    TASK_CDC_CHANNEL: "integrations.sync_salesforce_tasks",
    CONTENT_DOCUMENT_CDC_CHANNEL: "integrations.sync_salesforce_notes",
    CONTENT_DOCUMENT_LINK_CDC_CHANNEL: "integrations.sync_salesforce_notes",
}


@app.task(
    name=_TASK_NAME,
    bind=True,
    max_retries=_L["retries"],
    acks_late=True,
    soft_time_limit=_L["soft"],
    time_limit=_L["hard"],
)
def run_salesforce_cdc_subscriber_task(
    self: Task,
    organisation_id: str | None = None,
    integration_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Run one explicit Salesforce CDC stream."""

    _ = self

    async def _run() -> dict[str, Any]:
        if organisation_id is None or integration_id is None:
            raise ValueError("organisation_id and integration_id are required")

        org_id = UUID(organisation_id)
        int_id = UUID(integration_id)
        cdc_channel = channel or OPPORTUNITY_CDC_CHANNEL
        sync_id: UUID | None = None
        async for session in acquire_org_async_session(org_id):
            sync_id = await SyncHistoryManager.start_fresh(session, int_id)

        try:
            result = await _run_subscriber(
                organisation_id=org_id,
                integration_id=int_id,
                channel=cdc_channel,
                sync_id=sync_id,
            )
        except Exception as exc:
            await _complete_history(
                organisation_id=org_id,
                sync_id=sync_id,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=1,
                metadata={"provider": "salesforce", "channel": cdc_channel},
                error_details={"class": type(exc).__name__, "message": str(exc)},
            )
            raise

        await _complete_history(
            organisation_id=org_id,
            sync_id=sync_id,
            status=SyncStatus.SUCCESS,
            records_processed=int(result.get("records_processed") or 0),
            metadata={
                "provider": "salesforce",
                "channel": cdc_channel,
                "events_seen": result.get("events_seen"),
                "records_processed": result.get("records_processed"),
                "latest_replay_id": result.get("latest_replay_id"),
            },
        )
        return result

    return run_async(_run())


async def _run_subscriber(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    channel: str,
    sync_id: UUID | None = None,
) -> dict[str, Any]:
    _ = sync_id
    lease_owner = str(uuid4())
    state_service = SalesforceCdcSubscriptionStateService()
    async for session in acquire_org_async_session(organisation_id):
        integration = await _load_integration(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        try:
            metadata = (
                get_salesforce_opportunity_cdc_metadata(integration)
                if channel == OPPORTUNITY_CDC_CHANNEL
                else get_salesforce_cdc_metadata_for_channel(integration, channel=channel)
            )
        except SalesforceCdcMetadataError as exc:
            try:
                enabled_metadata = get_salesforce_opportunity_cdc_metadata(integration)
            except SalesforceCdcMetadataError:
                raise exc from None
            raise ValueError(
                f"Requested channel {channel} does not match enabled Salesforce CDC channel "
                f"{enabled_metadata.channel}"
            ) from exc
        if channel != metadata.channel:
            raise ValueError(
                f"Requested channel {channel} does not match enabled Salesforce CDC channel "
                f"{metadata.channel}"
            )
        lease = await state_service.acquire_lease(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            salesforce_organization_id=metadata.salesforce_organization_id,
            lease_owner=lease_owner,
            lease_seconds=_LEASE_SECONDS,
            pubsub_endpoint=metadata.pubsub_endpoint,
        )
        if not lease.acquired:
            return {"status": "lease_unavailable", "events_seen": 0, "records_processed": 0}
        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        instance_url = metadata.instance_url
        salesforce_organization_id = metadata.salesforce_organization_id
        pubsub_endpoint = metadata.pubsub_endpoint
        stored_replay_id = lease.subscription.last_replay_id

    replay_id = decode_replay_id(stored_replay_id) if stored_replay_id else None
    events_seen = 0
    records_processed = 0
    latest_replay_id: str | None = None

    try:
        token_info.access_token = await get_access_token(token_info)
        async for event in SalesforcePubSubClient(pubsub_endpoint=pubsub_endpoint).subscribe(
            token_info=token_info,
            instance_url=instance_url,
            salesforce_organization_id=salesforce_organization_id,
            channel=channel,
            replay_id=replay_id,
            max_runtime_seconds=_MAX_RUNTIME_SECONDS,
        ):
            if isinstance(event, SalesforcePubSubKeepalive):
                await _store_keepalive_and_renew_lease(
                    state_service=state_service,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    channel=channel,
                    lease_owner=lease_owner,
                    replay_id=encode_replay_id(event.replay_id),
                )
                continue

            change_event = decode_salesforce_change_event(
                replay_id=event.replay_id,
                schema_id=event.schema_id,
                payload=event.payload,
                object_name=getattr(metadata, "object_name", "Opportunity"),
                id_prefix=getattr(metadata, "id_prefix", "006"),
            )
            for record_id in change_event.record_ids:
                await _process_record_change(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    record_id=record_id,
                    change_type=change_event.change_type,
                    instance_url=instance_url,
                    channel=channel,
                )
                records_processed += 1

            events_seen += 1
            latest_replay_id = encode_replay_id(event.replay_id)
            await _store_replay_and_renew_lease(
                state_service=state_service,
                organisation_id=organisation_id,
                integration_id=integration_id,
                channel=channel,
                lease_owner=lease_owner,
                replay_id=latest_replay_id,
            )
    except SalesforcePubSubReplayIdError as exc:
        async for session in acquire_org_async_session(organisation_id):
            await state_service.mark_error(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                channel=channel,
                error=exc,
            )
        async for session in acquire_org_async_session(organisation_id):
            await state_service.clear_replay_for_repair(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                channel=channel,
            )
        await _release_lease(
            state_service=state_service,
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            lease_owner=lease_owner,
        )
        await _enqueue_repair_sync(
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
        )
        return {
            "status": "replay_expired_repair_queued",
            "events_seen": events_seen,
            "records_processed": records_processed,
            "latest_replay_id": latest_replay_id,
        }
    except Exception as exc:
        async for session in acquire_org_async_session(organisation_id):
            await state_service.mark_error(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                channel=channel,
                error=exc,
            )
        await _release_lease(
            state_service=state_service,
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            lease_owner=lease_owner,
        )
        await _enqueue_next_subscriber_run(
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            countdown=_FAILURE_REQUEUE_DELAY_SECONDS,
        )
        raise

    await _release_lease(
        state_service=state_service,
        organisation_id=organisation_id,
        integration_id=integration_id,
        channel=channel,
        lease_owner=lease_owner,
    )
    await _enqueue_next_subscriber_run(
        organisation_id=organisation_id,
        integration_id=integration_id,
        channel=channel,
    )
    return {
        "status": "completed",
        "events_seen": events_seen,
        "records_processed": records_processed,
        "latest_replay_id": latest_replay_id,
    }


async def _enqueue_repair_sync(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    channel: str,
) -> None:
    task_name = _REPAIR_TASK_BY_CHANNEL[channel]
    await send_integrations_task_async(
        task_name,
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "start_cdc_after_success": True,
            "cdc_channel": channel,
        },
        queue="integrations_priority",
    )


async def _process_record_change(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    record_id: str,
    change_type: str,
    instance_url: str,
    channel: str,
) -> None:
    if channel == OPPORTUNITY_CDC_CHANNEL:
        await _process_opportunity_record_change(
            organisation_id=organisation_id,
            integration_id=integration_id,
            record_id=record_id,
            change_type=change_type,
            instance_url=instance_url,
        )
        return
    if channel == ACCOUNT_CDC_CHANNEL:
        await _process_account_record_change(
            organisation_id=organisation_id,
            integration_id=integration_id,
            record_id=record_id,
            change_type=change_type,
            instance_url=instance_url,
        )
        return
    if channel == CONTACT_CDC_CHANNEL:
        await _process_contact_record_change(
            organisation_id=organisation_id,
            integration_id=integration_id,
            record_id=record_id,
            change_type=change_type,
            instance_url=instance_url,
        )
        return
    if channel == USER_CDC_CHANNEL:
        await _process_user_record_change(
            organisation_id=organisation_id,
            integration_id=integration_id,
            record_id=record_id,
            change_type=change_type,
            instance_url=instance_url,
        )
        return
    if channel == TASK_CDC_CHANNEL:
        await _process_task_record_change(
            organisation_id=organisation_id,
            integration_id=integration_id,
            record_id=record_id,
            change_type=change_type,
            instance_url=instance_url,
        )
        return
    if channel == CONTENT_DOCUMENT_CDC_CHANNEL:
        await _process_content_document_record_change(
            organisation_id=organisation_id,
            integration_id=integration_id,
            record_id=record_id,
            change_type=change_type,
            instance_url=instance_url,
        )
        return
    if channel == CONTENT_DOCUMENT_LINK_CDC_CHANNEL:
        await _process_content_document_link_record_change(
            organisation_id=organisation_id,
            integration_id=integration_id,
            record_id=record_id,
            change_type=change_type,
            instance_url=instance_url,
        )
        return
    raise ValueError(f"Unsupported Salesforce CDC channel: {channel}")


async def _process_opportunity_record_change(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    record_id: str,
    change_type: str,
    instance_url: str,
) -> None:
    if change_type.upper() in _DELETE_CHANGE_TYPES:
        await _soft_delete_deal(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return

    service = SalesforceDealRecordSyncService()
    context = await service._load_context(organisation_id, integration_id)
    record = await SalesforceSingleOpportunityFetcher(
        token_info=context.token_info,
        instance_url=instance_url,
        opportunity_id=record_id,
    ).fetch()
    if record is None:
        await _soft_delete_deal(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return
    await service.persist_single_opportunity(
        organisation_id=organisation_id,
        integration_id=integration_id,
        raw_opportunity=record,
        instance_url=instance_url,
    )


async def _process_account_record_change(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    record_id: str,
    change_type: str,
    instance_url: str,
) -> None:
    if change_type.upper() in _DELETE_CHANGE_TYPES:
        await _deactivate_company(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return

    service = SalesforceCompanyRecordSyncService()
    context = await service._load_context(organisation_id, integration_id)
    record = await SalesforceAccountsFetcher(
        token_info=context.token_info,
        instance_url=instance_url,
    ).fetch_one(record_id)
    if record is None:
        await _deactivate_company(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return
    await service.persist_single_account(
        organisation_id=organisation_id,
        integration_id=integration_id,
        raw_account=record,
    )


async def _process_contact_record_change(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    record_id: str,
    change_type: str,
    instance_url: str,
) -> None:
    if change_type.upper() in _DELETE_CHANGE_TYPES:
        await _soft_delete_contact(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return

    service = SalesforceContactRecordSyncService()
    context = await service._load_context(organisation_id, integration_id)
    record = await SalesforceContactsFetcher(
        token_info=context.token_info,
        instance_url=instance_url,
    ).fetch_one(record_id)
    if record is None:
        await _soft_delete_contact(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return
    await service.persist_single_contact(
        organisation_id=organisation_id,
        integration_id=integration_id,
        raw_contact=record,
    )


async def _process_task_record_change(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    record_id: str,
    change_type: str,
    instance_url: str,
) -> None:
    if change_type.upper() in _DELETE_CHANGE_TYPES:
        await _soft_delete_task(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return

    service = SalesforceTaskRecordSyncService()
    context = await service._load_context(organisation_id, integration_id)
    record = await SalesforceTasksFetcher(
        token_info=context.token_info,
        instance_url=instance_url,
    ).fetch_one(record_id)
    if record is None:
        await _soft_delete_task(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return
    await service.persist_single_task(
        organisation_id=organisation_id,
        integration_id=integration_id,
        raw_task=record,
    )


async def _process_user_record_change(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    record_id: str,
    change_type: str,
    instance_url: str,
) -> None:
    if change_type.upper() in _DELETE_CHANGE_TYPES:
        await _deactivate_user(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return

    service = SalesforceUserRecordSyncService()
    context = await service._load_context(organisation_id, integration_id)
    record = await SalesforceUsersFetcher(
        token_info=context.token_info,
        instance_url=instance_url,
    ).fetch_one(record_id)
    if record is None:
        await _deactivate_user(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return
    await service.persist_single_user(
        organisation_id=organisation_id,
        integration_id=integration_id,
        raw_user=record,
    )


async def _process_content_document_record_change(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    record_id: str,
    change_type: str,
    instance_url: str,
) -> None:
    if change_type.upper() in _DELETE_CHANGE_TYPES:
        await _soft_delete_note(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return

    service = SalesforceNoteRecordSyncService()
    context = await service._load_context(organisation_id, integration_id)
    linked_entity_ids = await service._load_linked_entity_ids(organisation_id, integration_id)
    records = await SalesforceNotesFetcher(
        token_info=context.token_info,
        instance_url=instance_url,
    ).fetch_one_by_note_id(record_id, linked_entity_ids=linked_entity_ids)
    if not records:
        await _soft_delete_note(
            organisation_id=organisation_id,
            integration_id=integration_id,
            external_id=record_id,
        )
        return
    await service.persist_single_note(
        organisation_id=organisation_id,
        integration_id=integration_id,
        raw_notes=records,
    )


async def _process_content_document_link_record_change(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    record_id: str,
    change_type: str,
    instance_url: str,
) -> None:
    _ = change_type
    service = SalesforceNoteRecordSyncService()
    context = await service._load_context(organisation_id, integration_id)
    linked_entity_ids = await service._load_linked_entity_ids(organisation_id, integration_id)
    fetcher = SalesforceNotesFetcher(
        token_info=context.token_info,
        instance_url=instance_url,
    )
    records = await fetcher.fetch_one_by_link_id(record_id, linked_entity_ids=linked_entity_ids)
    if not records:
        if change_type.upper() not in _DELETE_CHANGE_TYPES:
            return
        link_row = await fetcher.fetch_link_by_id(record_id)
        content_document_id = (
            link_row.get("ContentDocumentId") if isinstance(link_row, dict) else None
        )
        linked_entity_id = link_row.get("LinkedEntityId") if isinstance(link_row, dict) else None
        if (
            isinstance(content_document_id, str)
            and content_document_id
            and isinstance(linked_entity_id, str)
            and linked_entity_id in linked_entity_ids
        ):
            remaining_records = await fetcher.fetch_one_by_note_id(
                content_document_id,
                linked_entity_ids=linked_entity_ids,
            )
            if remaining_records:
                await service.persist_single_note(
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    raw_notes=remaining_records,
                )
                return
            await _soft_delete_note(
                organisation_id=organisation_id,
                integration_id=integration_id,
                external_id=content_document_id,
            )
            return
        if isinstance(linked_entity_id, str) and linked_entity_id not in linked_entity_ids:
            return
        await _soft_delete_note_by_content_document_link_id(
            organisation_id=organisation_id,
            integration_id=integration_id,
            content_document_link_id=record_id,
        )
        return
    await service.persist_single_note(
        organisation_id=organisation_id,
        integration_id=integration_id,
        raw_notes=records,
    )


async def _soft_delete_deal(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    external_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        deal_id = await _mark_source_mapping_deleted(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.DEAL,
            external_id=external_id,
        )
        if deal_id is None:
            return

        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.is_deleted) == False,  # noqa: E712
        )
        deal = (await session.execute(stmt)).scalar_one_or_none()
        if deal is None:
            return
        deal.is_deleted = True
        deal.last_seen_at = datetime.now(UTC)
        session.add(deal)
        await session.commit()
        return


async def _deactivate_company(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    external_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        company_id = await _mark_source_mapping_deleted(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.COMPANY,
            external_id=external_id,
        )
        if company_id is None:
            return

        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.company_id) == company_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        company = (await session.execute(stmt)).scalar_one_or_none()
        if company is None:
            return
        company.is_active = False
        company.last_seen_at = datetime.now(UTC)
        session.add(company)
        await session.commit()
        return


async def _soft_delete_contact(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    external_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        contact_id = await _mark_source_mapping_deleted(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.CONTACT,
            external_id=external_id,
        )
        if contact_id is None:
            return

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        contact = (await session.execute(stmt)).scalar_one_or_none()
        if contact is None:
            return
        contact.is_deleted = True
        contact.last_seen_at = datetime.now(UTC)
        session.add(contact)
        await session.commit()
        return


async def _soft_delete_task(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    external_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        task_id = await _resolve_deleted_source_id(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.TASK,
            external_id=external_id,
        )
        if task_id is None:
            return

        stmt = select(CRMTask).where(
            col(CRMTask.organisation_id) == organisation_id,
            col(CRMTask.integration_id) == integration_id,
            col(CRMTask.task_id) == task_id,
        )
        task = (await session.execute(stmt)).scalar_one_or_none()
        if task is not None:
            task.is_deleted = True
            task.updated_at = datetime.now(UTC)
            session.add(task)
        await ActivityFeedProjectionService(
            session, organisation_id
        ).soft_delete_activity_projection(task_id)
        await session.commit()
        return


async def _deactivate_user(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    external_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        user_id = await _mark_source_mapping_deleted(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.USER,
            external_id=external_id,
        )
        if user_id is None:
            return

        stmt = select(CRMUser).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.user_id) == user_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
        )
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            return
        user.is_active = False
        user.last_seen_at = datetime.now(UTC)
        session.add(user)
        await session.commit()
        return


async def _soft_delete_note(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    external_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        note_id = await _resolve_deleted_source_id(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.NOTE,
            external_id=external_id,
        )
        if note_id is None:
            return

        stmt = select(CRMNote).where(
            col(CRMNote.organisation_id) == organisation_id,
            col(CRMNote.integration_id) == integration_id,
            col(CRMNote.note_id) == note_id,
        )
        note = (await session.execute(stmt)).scalar_one_or_none()
        if note is not None:
            note.is_deleted = True
            note.updated_at = datetime.now(UTC)
            session.add(note)
        await ActivityFeedProjectionService(
            session, organisation_id
        ).soft_delete_activity_projection(note_id)
        await session.commit()
        return


async def _soft_delete_note_by_content_document_link_id(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    content_document_link_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        note_id = await _resolve_deleted_source_id(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            object_type=CRMSourceObjectType.NOTE_LINK,
            external_id=content_document_link_id,
        )
        if note_id is None:
            return

        stmt = select(CRMNote).where(
            col(CRMNote.organisation_id) == organisation_id,
            col(CRMNote.integration_id) == integration_id,
            col(CRMNote.note_id) == note_id,
        )
        note = (await session.execute(stmt)).scalar_one_or_none()
        if note is not None:
            note.is_deleted = True
            note.updated_at = datetime.now(UTC)
            session.add(note)
        await ActivityFeedProjectionService(
            session, organisation_id
        ).soft_delete_activity_projection(note_id)
        await session.commit()
        return


async def _mark_source_mapping_deleted(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    object_type: CRMSourceObjectType,
    external_id: str,
) -> UUID | None:
    local_ids = await CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    ).mark_external_ids_deleted(
        object_type=object_type,
        external_ids=[external_id],
    )
    return local_ids.get(external_id.strip())


async def _resolve_deleted_source_id(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
    object_type: CRMSourceObjectType,
    external_id: str,
) -> UUID | None:
    local_id = await _mark_source_mapping_deleted(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_type=object_type,
        external_id=external_id,
    )
    if local_id is not None:
        return local_id

    local_ids = await CRMSourceMappingHelper(
        session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        provider="salesforce",
    ).load_local_id_map(
        object_type=object_type,
        external_ids=[external_id],
        include_deleted=True,
    )
    return local_ids.get(external_id.strip())


async def _store_keepalive_and_renew_lease(
    *,
    state_service: SalesforceCdcSubscriptionStateService,
    organisation_id: UUID,
    integration_id: UUID,
    channel: str,
    lease_owner: str,
    replay_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        stored = await state_service.store_keepalive_replay(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            replay_id=replay_id,
            lease_owner=lease_owner,
        )
        renewed = await state_service.renew_lease(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            lease_owner=lease_owner,
            lease_seconds=_LEASE_SECONDS,
        )
        if not stored or not renewed:
            raise RuntimeError("Salesforce CDC subscriber lost its lease")
        return


async def _store_replay_and_renew_lease(
    *,
    state_service: SalesforceCdcSubscriptionStateService,
    organisation_id: UUID,
    integration_id: UUID,
    channel: str,
    lease_owner: str,
    replay_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        stored = await state_service.store_replay_after_commit(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            replay_id=replay_id,
            lease_owner=lease_owner,
        )
        renewed = await state_service.renew_lease(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            lease_owner=lease_owner,
            lease_seconds=_LEASE_SECONDS,
        )
        if not stored or not renewed:
            raise RuntimeError("Salesforce CDC subscriber lost its lease")
        return


async def _release_lease(
    *,
    state_service: SalesforceCdcSubscriptionStateService,
    organisation_id: UUID,
    integration_id: UUID,
    channel: str,
    lease_owner: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        await state_service.release_lease(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            channel=channel,
            lease_owner=lease_owner,
        )
        return


async def _enqueue_next_subscriber_run(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    channel: str,
    countdown: int | None = None,
) -> None:
    send_kwargs: dict[str, Any] = {
        "task_name": _TASK_NAME,
        "kwargs": {
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "channel": channel,
        },
        "queue": "integrations_streaming",
        "routing_key": "integrations_streaming",
    }
    if countdown is not None:
        send_kwargs["countdown"] = countdown
    await send_integrations_task_async(**send_kwargs)


async def _complete_history(
    *,
    organisation_id: UUID,
    sync_id: UUID | None,
    status: SyncStatus,
    records_processed: int,
    records_failed: int = 0,
    metadata: dict[str, Any] | None = None,
    error_details: dict[str, Any] | None = None,
) -> None:
    if sync_id is None:
        return
    async for session in acquire_org_async_session(organisation_id):
        await SyncHistoryManager.complete_fresh(
            session,
            sync_id,
            status=status,
            records_processed=records_processed,
            records_failed=records_failed,
            metadata=metadata,
            error_details=error_details,
        )
        return


async def _load_integration(
    *,
    session,
    organisation_id: UUID,
    integration_id: UUID,
) -> KnowledgeIntegration:
    stmt = select(KnowledgeIntegration).where(
        col(KnowledgeIntegration.organisation_id) == organisation_id,
        col(KnowledgeIntegration.integration_id) == integration_id,
    )
    integration = (await session.execute(stmt)).scalar_one_or_none()
    if integration is None:
        raise ValueError(f"Salesforce integration {integration_id} not found")
    return cast(KnowledgeIntegration, integration)
