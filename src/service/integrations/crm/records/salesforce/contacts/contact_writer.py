"""Upsert writer for CRM contacts from Salesforce Contact rows."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import (
    CRMSourceMappingHelper,
    IntegrationMappingUpsert,
)
from src.service.integrations.crm.records.salesforce.contacts.contact_types import (
    ContactWriteResult,
    NormalizedSalesforceContact,
)

logger = get_logger(__name__)


class SalesforceContactWriter:
    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id
        self.source_mappings = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="salesforce",
        )

    async def apply(
        self,
        *,
        normalized: list[NormalizedSalesforceContact],
        company_map: dict[str, UUID],
        owner_map: dict[str, UUID],
    ) -> ContactWriteResult:
        result = ContactWriteResult()
        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows(
            external_ids={contact.external_id for contact in normalized}
        )
        mapping_rows: list[IntegrationMappingUpsert] = []

        for contact in normalized:
            resolved_company_id = self._resolve_company_id(
                contact.company_external_id, company_map, result
            )
            resolved_owner_id = self._resolve_owner_id(contact.owner_external_id, owner_map, result)
            existing = existing_by_external.get(contact.external_id)
            if existing is None:
                row = self._new_row(contact, resolved_company_id, resolved_owner_id, now=now)
                self.session.add(row)
                mapping_rows.append(self._source_mapping_row(contact, row.contact_id, now=now))
                result.contacts_created += 1
            else:
                was_deleted = existing.is_deleted
                self._apply_updates(
                    existing,
                    contact,
                    resolved_company_id,
                    resolved_owner_id,
                    now=now,
                )
                mapping_rows.append(self._source_mapping_row(contact, existing.contact_id, now=now))
                if was_deleted:
                    existing.is_deleted = False
                    result.contacts_reactivated += 1
                else:
                    result.contacts_updated += 1

        if mapping_rows:
            await self.source_mappings.bulk_upsert_integration_mappings(mapping_rows)

        await self.session.flush()
        logger.info(
            "Salesforce contact writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "contacts_created": result.contacts_created,
                "contacts_updated": result.contacts_updated,
                "contacts_reactivated": result.contacts_reactivated,
                "unresolved_company_link": result.unresolved_company_link,
                "unresolved_owner_link": result.unresolved_owner_link,
            },
        )
        return result

    def _resolve_company_id(
        self,
        company_external_id: str | None,
        company_map: dict[str, UUID],
        result: ContactWriteResult,
    ) -> UUID | None:
        if not company_external_id:
            return None
        resolved = company_map.get(company_external_id)
        if resolved is None:
            result.unresolved_company_link += 1
            return None
        return resolved

    def _resolve_owner_id(
        self,
        owner_external_id: str | None,
        owner_map: dict[str, UUID],
        result: ContactWriteResult,
    ) -> UUID | None:
        if not owner_external_id:
            return None
        resolved = owner_map.get(owner_external_id)
        if resolved is None:
            result.unresolved_owner_link += 1
            return None
        return resolved

    async def _load_existing_rows(
        self, external_ids: set[str] | None = None
    ) -> dict[str, CRMContact]:
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.CONTACT,
            external_ids=external_ids,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.integration_id) == self.integration_id,
            col(CRMContact.contact_id).in_(set(external_to_local.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.contact_id]: row
            for row in rows
            if row.contact_id in local_to_external
        }

    def _source_mapping_row(
        self,
        contact: NormalizedSalesforceContact,
        local_contact_id: UUID,
        *,
        now: datetime,
    ) -> IntegrationMappingUpsert:
        return IntegrationMappingUpsert(
            external_object_type=CRMSourceObjectType.CONTACT,
            external_id=contact.external_id,
            local_object_type=CRMSourceObjectType.CONTACT,
            local_object_id=local_contact_id,
            data_payload=contact.data_payload,
            source_created_at=contact.source_created_at,
            source_modified_at=contact.source_modified_at,
            synced_at=now,
        )

    def _new_row(
        self,
        contact: NormalizedSalesforceContact,
        resolved_company_id: UUID | None,
        resolved_owner_id: UUID | None,
        *,
        now: datetime,
    ) -> CRMContact:
        return CRMContact(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=contact.full_name,
            job_title=contact.job_title,
            company_name=contact.company_name,
            company_id=resolved_company_id,
            email=contact.email,
            additional_emails=list(contact.additional_emails),
            phone=contact.phone,
            mobile_phone=contact.mobile_phone,
            fax=contact.fax,
            website=contact.website,
            owner_id=resolved_owner_id,
            lifecycle_stage=contact.lifecycle_stage,
            lead_status=contact.lead_status,
            marketing_status=contact.marketing_status,
            address=contact.address,
            city=contact.city,
            state=contact.state,
            postal_code=contact.postal_code,
            country=contact.country,
            timezone=contact.timezone,
            last_contacted_at=contact.last_contacted_at,
            last_activity_at=contact.last_activity_at,
            source_created_at=contact.source_created_at,
            source_modified_at=contact.source_modified_at,
            synced_at=now,
            last_seen_at=now,
            properties=contact.properties,
        )

    def _apply_updates(
        self,
        row: CRMContact,
        contact: NormalizedSalesforceContact,
        resolved_company_id: UUID | None,
        resolved_owner_id: UUID | None,
        *,
        now: datetime,
    ) -> None:
        row.first_name = contact.first_name
        row.last_name = contact.last_name
        row.full_name = contact.full_name
        row.job_title = contact.job_title
        row.company_name = contact.company_name
        row.company_id = resolved_company_id
        row.email = contact.email
        row.additional_emails = list(contact.additional_emails)
        row.phone = contact.phone
        row.mobile_phone = contact.mobile_phone
        row.fax = contact.fax
        row.website = contact.website
        row.owner_id = resolved_owner_id
        row.lifecycle_stage = contact.lifecycle_stage
        row.lead_status = contact.lead_status
        row.marketing_status = contact.marketing_status
        row.address = contact.address
        row.city = contact.city
        row.state = contact.state
        row.postal_code = contact.postal_code
        row.country = contact.country
        row.timezone = contact.timezone
        row.last_contacted_at = contact.last_contacted_at
        row.last_activity_at = contact.last_activity_at
        row.source_created_at = contact.source_created_at
        row.source_modified_at = contact.source_modified_at
        row.synced_at = now
        row.last_seen_at = now
        row.properties = contact.properties
