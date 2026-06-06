"""Upsert writer for CRM contacts from HubSpot.

Scoped to `(organisation_id, integration_id, is_deleted=false)` on every
statement. One pass: upsert every normalised contact by source mapping;
resolve `company_id` via the pre-loaded `company_map`
(`{company_external_id → company_id}`) from the same session. Unresolved
links leave `company_id=NULL` and tick a counter for visibility.

No hard delete. `contact_id` PK is never rewritten — CRMDeal.primary_contact_id
references it. CRMContact has no `is_active` column, so there is no
active-snapshot deactivation pass here (deferred until the column is added).
"""

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
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.contacts.contact_types import (
    ContactWriteResult,
    NormalizedHubSpotContact,
)

logger = get_logger(__name__)


class HubSpotContactWriter:
    """Writer for crm_contacts rows from HubSpot contacts."""

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
            provider="hubspot",
        )

    async def apply(
        self,
        *,
        normalized: list[NormalizedHubSpotContact],
        company_map: dict[str, UUID],
        owner_map: dict[str, UUID],
    ) -> ContactWriteResult:
        """Upsert every normalised contact.

        `CRMContact` has no `is_active` column today — no deactivation pass
        and no `all_external_ids` parameter. See ContactWriteResult docstring.
        """
        result = ContactWriteResult()
        now = datetime.now(UTC)

        existing_by_external = await self._load_existing_rows()

        for contact in normalized:
            resolved_company_id = self._resolve_company_id(
                contact.company_external_id, company_map, result
            )
            resolved_owner_id = self._resolve_owner_id(contact.owner_external_id, owner_map, result)
            existing = existing_by_external.get(contact.external_id)
            if existing is None:
                row = self._new_row(contact, resolved_company_id, resolved_owner_id, now=now)
                self.session.add(row)
                await self._upsert_source_mapping(contact, row.contact_id, now=now)
                result.contacts_created += 1
            elif not existing.is_deleted and _has_same_or_newer_source_modified(existing, contact):
                result.contacts_skipped_stale += 1
            else:
                was_deleted = existing.is_deleted
                self._apply_updates(
                    existing,
                    contact,
                    resolved_company_id,
                    resolved_owner_id,
                    now=now,
                )
                if was_deleted:
                    existing.is_deleted = False
                    result.contacts_reactivated += 1
                else:
                    result.contacts_updated += 1
                await self._upsert_source_mapping(contact, existing.contact_id, now=now)

        await self.session.flush()
        logger.info(
            "Contact writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "contacts_created": result.contacts_created,
                "contacts_updated": result.contacts_updated,
                "contacts_reactivated": result.contacts_reactivated,
                "contacts_skipped_stale": result.contacts_skipped_stale,
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

    async def _load_existing_rows(self) -> dict[str, CRMContact]:
        """Every mapped contact row for this integration, keyed by provider id."""
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.CONTACT,
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

    async def _upsert_source_mapping(
        self,
        contact: NormalizedHubSpotContact,
        local_contact_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
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
        contact: NormalizedHubSpotContact,
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
        contact: NormalizedHubSpotContact,
        resolved_company_id: UUID | None,
        resolved_owner_id: UUID | None,
        *,
        now: datetime,
    ) -> None:
        # contact_id PK is referenced by CRMDeal.primary_contact_id — do NOT
        # rewrite it. `company_id` and `owner_id` are re-resolved every run so
        # ownership / account assignment changes in HubSpot propagate.
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


def _has_same_or_newer_source_modified(
    existing: CRMContact,
    incoming: NormalizedHubSpotContact,
) -> bool:
    if existing.source_modified_at is None or incoming.source_modified_at is None:
        return False
    return existing.source_modified_at >= incoming.source_modified_at
