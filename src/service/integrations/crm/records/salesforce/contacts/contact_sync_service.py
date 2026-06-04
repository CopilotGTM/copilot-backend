"""Salesforce contacts record sync orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.organisation_tables.sync import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.backfill import (
    DealFkBackfillOp,
    backfill_crm_deals,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_normalizer import (
    normalize_contacts,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_types import (
    ContactSyncResult,
    ContactWriteResult,
    NormalizedSalesforceContact,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_writer import (
    SalesforceContactWriter,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
)
from src.workers.integrations_worker.providers.crm.salesforce.contacts_fetcher import (
    SalesforceContactsFetcher,
)

logger = get_logger(__name__)

_CONTACT_PERSIST_CHUNK_SIZE = 500
_DEAL_BACKFILL_REFERENCE_CHUNK_SIZE = 1000


@dataclass(slots=True)
class SalesforceContactSyncContext:
    token_info: IntegrationTokenInfo
    instance_url: str


class SalesforceContactSyncConfigurationError(ValueError):
    """Integration not usable for Salesforce contacts record sync."""


class SalesforceContactRecordSyncService:
    PROVIDER = "salesforce"
    OBJECT_TYPE = "contact"

    async def run(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> ContactSyncResult:
        started_at = datetime.now(UTC)
        context = await self._load_context(organisation_id, integration_id)

        fetcher = SalesforceContactsFetcher(
            token_info=context.token_info,
            instance_url=context.instance_url,
        )
        pending_raw: list[dict[str, Any]] = []
        fetched_contacts = 0
        skipped_malformed = 0
        write_result = ContactWriteResult()

        async for page_records in fetcher.fetch_pages():
            fetched_contacts += len(page_records)
            pending_raw.extend(page_records)
            if len(pending_raw) < _CONTACT_PERSIST_CHUNK_SIZE:
                continue
            chunk_result = await self._persist_raw_chunk(
                organisation_id=organisation_id,
                integration_id=integration_id,
                raw_contacts=pending_raw,
            )
            self._merge_write_result(write_result, chunk_result[0])
            skipped_malformed += chunk_result[1]
            pending_raw = []

        if pending_raw:
            chunk_result = await self._persist_raw_chunk(
                organisation_id=organisation_id,
                integration_id=integration_id,
                raw_contacts=pending_raw,
            )
            self._merge_write_result(write_result, chunk_result[0])
            skipped_malformed += chunk_result[1]

        backfill_payload = await self._backfill_deal_primary_contacts(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )

        completed_at = datetime.now(UTC)
        return ContactSyncResult(
            provider=self.PROVIDER,
            object_type=self.OBJECT_TYPE,
            organisation_id=organisation_id,
            integration_id=integration_id,
            started_at=started_at,
            completed_at=completed_at,
            fetched_contacts=fetched_contacts,
            contacts_created=write_result.contacts_created,
            contacts_updated=write_result.contacts_updated,
            contacts_reactivated=write_result.contacts_reactivated,
            unresolved_company_link=write_result.unresolved_company_link,
            unresolved_owner_link=write_result.unresolved_owner_link,
            skipped_malformed=skipped_malformed,
            deal_backfill_rows_updated=backfill_payload["rows_updated"],
            deal_backfill_per_op=backfill_payload["per_op"],
        )

    async def persist_single_contact(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_contact: dict[str, Any],
    ) -> dict[str, Any]:
        normalized, skipped_malformed = normalize_contacts([raw_contact])
        write_result, backfill_result = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
            backfill_deals=True,
        )
        return {
            "contacts_created": write_result.contacts_created,
            "contacts_updated": write_result.contacts_updated,
            "contacts_reactivated": write_result.contacts_reactivated,
            "unresolved_company_link": write_result.unresolved_company_link,
            "unresolved_owner_link": write_result.unresolved_owner_link,
            "skipped_malformed": skipped_malformed,
            "deal_backfill_rows_updated": backfill_result["rows_updated"],
            "deal_backfill_per_op": backfill_result["per_op"],
        }

    async def _load_context(
        self,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> SalesforceContactSyncContext:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.organisation_id) == organisation_id,
            )
            integration = (await session.execute(stmt)).scalar_one_or_none()
            if integration is None:
                raise SalesforceContactSyncConfigurationError(
                    f"Integration {integration_id} not found for organisation {organisation_id}"
                )
            int_type = integration.integration_type
            if isinstance(int_type, str):
                int_type = IntegrationType(int_type)
            if int_type != IntegrationType.SALESFORCE:
                raise SalesforceContactSyncConfigurationError(
                    f"Integration {integration_id} is {int_type.value}, expected salesforce"
                )
            if not integration.is_active:
                raise SalesforceContactSyncConfigurationError(
                    f"Integration {integration_id} is not active"
                )
            credentials = dict(integration.credentials or {})
            instance_url = str(credentials.get("instance_url") or "").rstrip("/")
            if not instance_url:
                raise SalesforceContactSyncConfigurationError(
                    f"Integration {integration_id} is missing Salesforce instance_url"
                )
            return SalesforceContactSyncContext(
                token_info=IntegrationTokenInfo.from_integration(integration, organisation_id),
                instance_url=instance_url,
            )

        raise SalesforceContactSyncConfigurationError(
            "Could not acquire org session for Salesforce contacts record sync"
        )

    async def _persist_raw_chunk(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        raw_contacts: list[dict[str, Any]],
    ) -> tuple[ContactWriteResult, int, dict[str, Any]]:
        normalized, skipped_malformed = normalize_contacts(raw_contacts)
        logger.info(
            "Salesforce contacts chunk persist started",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "raw_contacts": len(raw_contacts),
                "normalized_contacts": len(normalized),
            },
        )
        write_result, backfill_payload = await self._persist(
            organisation_id=organisation_id,
            integration_id=integration_id,
            normalized=normalized,
            backfill_deals=False,
        )
        logger.info(
            "Salesforce contacts chunk persist completed",
            extra={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "contacts_created": write_result.contacts_created,
                "contacts_updated": write_result.contacts_updated,
                "contacts_reactivated": write_result.contacts_reactivated,
                "unresolved_company_link": write_result.unresolved_company_link,
                "unresolved_owner_link": write_result.unresolved_owner_link,
                "skipped_malformed": skipped_malformed,
                "deal_backfill_rows_updated": backfill_payload["rows_updated"],
            },
        )
        return write_result, skipped_malformed, backfill_payload

    async def _persist(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        normalized: list[NormalizedSalesforceContact],
        backfill_deals: bool = True,
    ) -> tuple[Any, dict[str, Any]]:
        async for session in acquire_org_async_session(organisation_id):
            company_external_ids = {
                contact.company_external_id for contact in normalized if contact.company_external_id
            }
            owner_external_ids = {
                contact.owner_external_id for contact in normalized if contact.owner_external_id
            }
            company_map = await self._load_company_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                external_ids=company_external_ids,
            )
            owner_map = await self._load_owner_map(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
                external_ids=owner_external_ids,
            )

            writer = SalesforceContactWriter(
                session=session,
                organisation_id=organisation_id,
                integration_id=integration_id,
            )
            write_result = await writer.apply(
                normalized=normalized,
                company_map=company_map,
                owner_map=owner_map,
            )

            backfill_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
            if backfill_deals:
                contact_pk_map = await self._load_contact_pk_map(
                    session=session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    external_ids=[contact.external_id for contact in normalized],
                )
                mapping: dict[str, tuple[UUID, str | None, None, str | None]] = {}
                for contact in normalized:
                    pk = contact_pk_map.get(contact.external_id)
                    if pk is None:
                        continue
                    mapping[contact.external_id] = (pk, contact.full_name, None, contact.email)

                if mapping:
                    ops = [
                        DealFkBackfillOp(
                            external_col="primary_contact_external_id",
                            fk_col="primary_contact_id",
                            name_col="primary_contact_name",
                            email_col="primary_contact_email",
                            mapping=mapping,
                        )
                    ]
                    backfill_result = await backfill_crm_deals(
                        session,
                        organisation_id=organisation_id,
                        integration_id=integration_id,
                        ops=ops,
                    )
                    backfill_payload["rows_updated"] = backfill_result.rows_updated
                    backfill_payload["per_op"] = dict(backfill_result.per_op)

            await session.commit()
            return write_result, backfill_payload

        raise SalesforceContactSyncConfigurationError(
            "Could not acquire org session for Salesforce contacts record sync write"
        )

    @staticmethod
    def _merge_write_result(target: ContactWriteResult, source: ContactWriteResult) -> None:
        target.contacts_created += source.contacts_created
        target.contacts_updated += source.contacts_updated
        target.contacts_reactivated += source.contacts_reactivated
        target.unresolved_company_link += source.unresolved_company_link
        target.unresolved_owner_link += source.unresolved_owner_link

    @staticmethod
    def _merge_backfill_payload(target: dict[str, Any], source: dict[str, Any]) -> None:
        target["rows_updated"] += source["rows_updated"]
        per_op = target["per_op"]
        for op_name, rows_updated in source["per_op"].items():
            per_op[op_name] = per_op.get(op_name, 0) + rows_updated

    async def _backfill_deal_primary_contacts(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
        after_external_id: str | None = None
        batch_number = 0

        while True:
            async for session in acquire_org_async_session(organisation_id):
                external_ids = await self._load_deal_primary_contact_external_ids(
                    session=session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    after_external_id=after_external_id,
                    limit=_DEAL_BACKFILL_REFERENCE_CHUNK_SIZE,
                )
                if not external_ids:
                    return payload

                mapping = await self._load_contact_backfill_map(
                    session=session,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    external_ids=external_ids,
                )
                batch_payload: dict[str, Any] = {"rows_updated": 0, "per_op": {}}
                if mapping:
                    backfill_result = await backfill_crm_deals(
                        session,
                        organisation_id=organisation_id,
                        integration_id=integration_id,
                        ops=[
                            DealFkBackfillOp(
                                external_col="primary_contact_external_id",
                                fk_col="primary_contact_id",
                                name_col="primary_contact_name",
                                email_col="primary_contact_email",
                                mapping=mapping,
                            )
                        ],
                    )
                    batch_payload["rows_updated"] = backfill_result.rows_updated
                    batch_payload["per_op"] = dict(backfill_result.per_op)
                    self._merge_backfill_payload(payload, batch_payload)

                await session.commit()
                batch_number += 1
                logger.info(
                    "Salesforce contact deal backfill batch completed",
                    extra={
                        "organisation_id": str(organisation_id),
                        "integration_id": str(integration_id),
                        "batch_number": batch_number,
                        "referenced_contact_ids": len(external_ids),
                        "resolved_contacts": len(mapping),
                        "rows_updated": batch_payload["rows_updated"],
                    },
                )
                after_external_id = external_ids[-1]
                break
            else:
                raise SalesforceContactSyncConfigurationError(
                    "Could not acquire org session for Salesforce contact deal backfill"
                )

    async def _load_deal_primary_contact_external_ids(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        after_external_id: str | None,
        limit: int,
    ) -> list[str]:
        external_col = col(CRMDeal.primary_contact_external_id)
        stmt = (
            select(CRMDeal.primary_contact_external_id)
            .where(
                col(CRMDeal.organisation_id) == organisation_id,
                col(CRMDeal.integration_id) == integration_id,
                col(CRMDeal.is_deleted) == False,  # noqa: E712
                external_col.is_not(None),
            )
            .distinct()
            .order_by(external_col)
            .limit(limit)
        )
        if after_external_id is not None:
            stmt = stmt.where(external_col > after_external_id)

        rows = (await session.execute(stmt)).all()
        return [external_id for (external_id,) in rows if external_id is not None]

    async def _load_contact_backfill_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: list[str],
    ) -> dict[str, tuple[UUID, str | None, None, str | None]]:
        if not external_ids:
            return {}
        stmt = select(
            CRMContact.external_id,
            CRMContact.contact_id,
            CRMContact.full_name,
            CRMContact.email,
        ).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
            col(CRMContact.external_id).in_(external_ids),
        )
        rows = (await session.execute(stmt)).all()
        return {
            external_id: (contact_id, full_name, None, email)
            for external_id, contact_id, full_name, email in rows
            if external_id is not None
        }

    async def _load_company_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, UUID]:
        if not external_ids:
            return {}
        stmt = select(CRMCompany.external_id, CRMCompany.company_id).where(
            col(CRMCompany.organisation_id) == organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
            col(CRMCompany.external_id).in_(external_ids),
        )
        rows = (await session.execute(stmt)).all()
        return {
            external_id: company_id for external_id, company_id in rows if external_id is not None
        }

    async def _load_owner_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: set[str],
    ) -> dict[str, UUID]:
        from src.domain.models.organisation_tables.sync.crm.users import CRMUser

        if not external_ids:
            return {}
        stmt = select(CRMUser.external_id, CRMUser.user_id).where(
            col(CRMUser.organisation_id) == organisation_id,
            col(CRMUser.integration_id) == integration_id,
            col(CRMUser.is_deleted) == False,  # noqa: E712
            col(CRMUser.external_id).in_(external_ids),
        )
        rows = (await session.execute(stmt)).all()
        return {external_id: user_id for external_id, user_id in rows if external_id is not None}

    async def _load_contact_pk_map(
        self,
        *,
        session,
        organisation_id: UUID,
        integration_id: UUID,
        external_ids: list[str] | None = None,
    ) -> dict[str, UUID]:
        stmt = select(CRMContact.external_id, CRMContact.contact_id).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712
        )
        if external_ids is not None:
            if not external_ids:
                return {}
            stmt = stmt.where(col(CRMContact.external_id).in_(external_ids))
        rows = (await session.execute(stmt)).all()
        return {
            external_id: contact_id for external_id, contact_id in rows if external_id is not None
        }
