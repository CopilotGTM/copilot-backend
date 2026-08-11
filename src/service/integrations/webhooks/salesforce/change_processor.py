"""Persist individual Salesforce CDC record changes for every subscriber runtime.

This module deliberately has no Celery dependency. Both the legacy bounded task and the
long-running aggregate connector call the same handlers so create, update, delete, and source
mapping behavior cannot drift between runtimes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import (
    CRMNote,
    CRMSourceObjectType,
    CRMTask,
)
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.infra.database.sessions import acquire_org_async_session
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
from src.service.integrations.webhooks.salesforce.metadata import (
    ACCOUNT_CDC_CHANNEL,
    CONTACT_CDC_CHANNEL,
    CONTENT_DOCUMENT_CDC_CHANNEL,
    CONTENT_DOCUMENT_LINK_CDC_CHANNEL,
    OPPORTUNITY_CDC_CHANNEL,
    TASK_CDC_CHANNEL,
    USER_CDC_CHANNEL,
)
from src.workers.integrations_worker.providers.crm.salesforce.accounts_fetcher import (
    SalesforceAccountsFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher import (
    SalesforceContactsFetcher,
)
from src.workers.integrations_worker.providers.crm.salesforce.notes_fetcher import (
    SalesforceNotesFetcher,
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

_DELETE_CHANGE_TYPES = {"DELETE"}


class SalesforceCdcChangeProcessor:
    """Route one decoded CDC record to its existing persistence handler."""

    async def process(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        entity_name: str,
        record_id: str,
        change_type: str,
        instance_url: str,
    ) -> None:
        """Process one supported Salesforce entity after its envelope is validated."""
        handler = _HANDLERS.get(entity_name)
        if handler is None:
            raise ValueError(f"Unsupported Salesforce CDC entity: {entity_name}")
        await handler(
            organisation_id=organisation_id,
            integration_id=integration_id,
            record_id=record_id,
            change_type=change_type,
            instance_url=instance_url,
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
    """Keep channel-based legacy tests on the same entity-based implementation."""
    entity_name = _ENTITY_BY_CHANNEL.get(channel)
    if entity_name is None:
        raise ValueError(f"Unsupported Salesforce CDC channel: {channel}")
    await SalesforceCdcChangeProcessor().process(
        organisation_id=organisation_id,
        integration_id=integration_id,
        entity_name=entity_name,
        record_id=record_id,
        change_type=change_type,
        instance_url=instance_url,
    )


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
        project_deal_created_activity_feed=change_type.upper() == "CREATE",
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
    *, organisation_id: UUID, integration_id: UUID, external_id: str
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
    *, organisation_id: UUID, integration_id: UUID, external_id: str
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
    *, organisation_id: UUID, integration_id: UUID, external_id: str
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
    *, organisation_id: UUID, integration_id: UUID, external_id: str
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
    *, organisation_id: UUID, integration_id: UUID, external_id: str
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
    *, organisation_id: UUID, integration_id: UUID, external_id: str
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
    *, organisation_id: UUID, integration_id: UUID, content_document_link_id: str
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
    session: Any,
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
    session: Any,
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


_HANDLERS = {
    "Opportunity": _process_opportunity_record_change,
    "Account": _process_account_record_change,
    "Contact": _process_contact_record_change,
    "Task": _process_task_record_change,
    "User": _process_user_record_change,
    "ContentDocument": _process_content_document_record_change,
    "ContentDocumentLink": _process_content_document_link_record_change,
}

_ENTITY_BY_CHANNEL = {
    OPPORTUNITY_CDC_CHANNEL: "Opportunity",
    ACCOUNT_CDC_CHANNEL: "Account",
    CONTACT_CDC_CHANNEL: "Contact",
    TASK_CDC_CHANNEL: "Task",
    USER_CDC_CHANNEL: "User",
    CONTENT_DOCUMENT_CDC_CHANNEL: "ContentDocument",
    CONTENT_DOCUMENT_LINK_CDC_CHANNEL: "ContentDocumentLink",
}
