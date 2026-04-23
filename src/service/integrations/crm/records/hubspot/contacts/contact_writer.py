"""Upsert writer for CRM contacts from HubSpot.

Scoped to `(organisation_id, integration_id, is_deleted=false)` on every
statement. One pass: upsert every normalised contact by `external_id`;
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
from src.infra.utils.logger import get_logger
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
                result.contacts_created += 1
            else:
                self._apply_updates(
                    existing,
                    contact,
                    resolved_company_id,
                    resolved_owner_id,
                    now=now,
                )
                result.contacts_updated += 1

        await self.session.flush()
        logger.info(
            "Contact writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "contacts_created": result.contacts_created,
                "contacts_updated": result.contacts_updated,
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
        """Every non-deleted crm_contacts row for this integration, keyed by external_id."""
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.integration_id) == self.integration_id,
            col(CRMContact.is_deleted) == False,  # noqa: E712 — SQL boolean
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {r.external_id: r for r in rows if r.external_id is not None}

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
            external_id=contact.external_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=contact.full_name,
            job_title=contact.job_title,
            company_name=contact.company_name,
            company_external_id=contact.company_external_id,
            company_id=resolved_company_id,
            email=contact.email,
            additional_emails=list(contact.additional_emails),
            phone=contact.phone,
            mobile_phone=contact.mobile_phone,
            fax=contact.fax,
            website=contact.website,
            owner_external_id=contact.owner_external_id,
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
            data_payload=contact.data_payload,
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
        row.company_external_id = contact.company_external_id
        row.company_id = resolved_company_id
        row.email = contact.email
        row.additional_emails = list(contact.additional_emails)
        row.phone = contact.phone
        row.mobile_phone = contact.mobile_phone
        row.fax = contact.fax
        row.website = contact.website
        row.owner_external_id = contact.owner_external_id
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
        row.data_payload = contact.data_payload
