"""Salesforce CDC subscriber task."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.database.sessions import (
    acquire_org_async_session,
)
from src.infra.utils.async_runner import run_async
from src.service.integrations.crm.records.salesforce.companies.company_sync_service import (
    SalesforceCompanyRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_sync_service import (
    SalesforceContactRecordSyncService,
)
from src.service.integrations.crm.records.salesforce.deals.deal_sync_service import (
    SalesforceDealRecordSyncService,
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
    OPPORTUNITY_CDC_CHANNEL,
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
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_client import (
    SalesforcePubSubClient,
    SalesforcePubSubKeepalive,
    SalesforcePubSubReplayIdError,
)
from src.workers.integrations_worker.providers.crm.salesforce.single_opportunity_fetcher import (
    SalesforceSingleOpportunityFetcher,
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


async def _soft_delete_deal(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    external_id: str,
) -> None:
    async for session in acquire_org_async_session(organisation_id):
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.external_id) == external_id,
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
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.external_id) == external_id,
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
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.external_id) == external_id,
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
