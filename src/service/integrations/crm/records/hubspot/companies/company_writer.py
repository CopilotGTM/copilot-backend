"""Active-snapshot writer for CRM companies from HubSpot.

Scoped to `(organisation_id, integration_id, is_deleted=false)` on every
statement. Two passes:

  Pass A — upsert every normalised company by source mapping.
    - existing row → update fields, mark active
    - no existing row → insert with a fresh company_id UUID

  Pass B — active-snapshot deactivation.
    - any prior mapped row whose provider id is not in `all_external_ids` (the full
      HubSpot snapshot we just fetched) gets is_active=false.

No hard delete. `company_id` PK is never rewritten — CRMDeal.company_id and
CRMContact.company_id reference it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.companies.company_types import (
    CompanyWriteResult,
    NormalizedHubSpotCompany,
)

logger = get_logger(__name__)


class HubSpotCompanyWriter:
    """Writer for crm_companies rows from HubSpot companies."""

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
        normalized: list[NormalizedHubSpotCompany],
        all_external_ids: Iterable[str],
        owner_map: dict[str, UUID],
    ) -> CompanyWriteResult:
        """Upsert every normalised company + deactivate absent rows.

        `owner_map` is `{owner_external_id → crm_users.user_id}` pre-loaded
        from the same session; the writer resolves `CRMCompany.owner_id` from
        it at write time. Unresolved links tick `unresolved_owner_link` and
        leave `owner_id=NULL`.
        """
        result = CompanyWriteResult()
        now = datetime.now(UTC)

        existing_by_external = await self._load_existing_rows()

        for company in normalized:
            existing = existing_by_external.get(company.external_id)
            if existing is None:
                resolved_owner_id = self._resolve_owner_id(
                    company.owner_external_id, owner_map, result
                )
                row = self._new_row(company, resolved_owner_id, now=now)
                self.session.add(row)
                await self._upsert_source_mapping(company, row.company_id, now=now)
                result.companies_created += 1
            elif _has_same_or_newer_source_modified(existing, company):
                result.companies_skipped_stale += 1
                result.skipped_stale_external_ids.add(company.external_id)
            else:
                resolved_owner_id = self._resolve_owner_id(
                    company.owner_external_id, owner_map, result
                )
                was_deleted = bool(existing.is_deleted)
                was_active = bool(existing.is_active)
                self._apply_updates(existing, company, resolved_owner_id, now=now)
                await self._upsert_source_mapping(company, existing.company_id, now=now)
                if was_deleted or not was_active:
                    existing.is_active = True
                    result.companies_reactivated += 1
                else:
                    result.companies_updated += 1

        snapshot_external_ids = set(all_external_ids)
        for external_id, row in existing_by_external.items():
            if external_id in snapshot_external_ids:
                continue
            if row.is_active:
                row.is_active = False
                result.companies_deactivated += 1
        await self.source_mappings.mark_missing_deleted(
            object_type=CRMSourceObjectType.COMPANY,
            seen_external_ids=snapshot_external_ids,
        )

        await self.session.flush()
        logger.info(
            "Company writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "companies_created": result.companies_created,
                "companies_updated": result.companies_updated,
                "companies_reactivated": result.companies_reactivated,
                "companies_deactivated": result.companies_deactivated,
                "companies_skipped_stale": result.companies_skipped_stale,
                "unresolved_owner_link": result.unresolved_owner_link,
            },
        )
        return result

    async def apply_single(
        self,
        *,
        company: NormalizedHubSpotCompany,
        owner_map: dict[str, UUID],
    ) -> CompanyWriteResult:
        """Upsert/reactivate one company without deactivating absent rows.

        Webhooks are object-level deltas, not full snapshots. Reusing
        `apply(..., all_external_ids=[company.external_id])` would incorrectly
        mark every other company inactive, so this method only touches the
        matched external id.
        """
        result = CompanyWriteResult()
        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows()
        existing = existing_by_external.get(company.external_id)
        if existing is None:
            resolved_owner_id = self._resolve_owner_id(company.owner_external_id, owner_map, result)
            row = self._new_row(company, resolved_owner_id, now=now)
            self.session.add(row)
            await self._upsert_source_mapping(company, row.company_id, now=now)
            result.companies_created += 1
        elif _has_same_or_newer_source_modified(existing, company):
            result.companies_skipped_stale += 1
            result.skipped_stale_external_ids.add(company.external_id)
        else:
            resolved_owner_id = self._resolve_owner_id(company.owner_external_id, owner_map, result)
            was_deleted = bool(existing.is_deleted)
            was_active = bool(existing.is_active)
            self._apply_updates(existing, company, resolved_owner_id, now=now)
            await self._upsert_source_mapping(company, existing.company_id, now=now)
            if was_deleted or not was_active:
                existing.is_active = True
                result.companies_reactivated += 1
            else:
                result.companies_updated += 1

        await self.session.flush()
        logger.info(
            "Company writer single apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "company_external_id": company.external_id,
                "companies_created": result.companies_created,
                "companies_updated": result.companies_updated,
                "companies_reactivated": result.companies_reactivated,
                "companies_skipped_stale": result.companies_skipped_stale,
                "unresolved_owner_link": result.unresolved_owner_link,
            },
        )
        return result

    def _resolve_owner_id(
        self,
        owner_external_id: str | None,
        owner_map: dict[str, UUID],
        result: CompanyWriteResult,
    ) -> UUID | None:
        if not owner_external_id:
            return None
        resolved = owner_map.get(owner_external_id)
        if resolved is None:
            result.unresolved_owner_link += 1
            return None
        return resolved

    async def _load_existing_rows(self) -> dict[str, CRMCompany]:
        """Every mapped company row for this integration, keyed by provider id."""
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.COMPANY,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.integration_id) == self.integration_id,
            col(CRMCompany.company_id).in_(set(external_to_local.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.company_id]: row
            for row in rows
            if row.company_id in local_to_external
        }

    async def _upsert_source_mapping(
        self,
        company: NormalizedHubSpotCompany,
        local_company_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
            external_object_type=CRMSourceObjectType.COMPANY,
            external_id=company.external_id,
            local_object_type=CRMSourceObjectType.COMPANY,
            local_object_id=local_company_id,
            data_payload=company.data_payload,
            source_created_at=company.source_created_at,
            source_modified_at=company.source_modified_at,
            synced_at=now,
        )

    def _new_row(
        self,
        company: NormalizedHubSpotCompany,
        resolved_owner_id: UUID | None,
        *,
        now: datetime,
    ) -> CRMCompany:
        return CRMCompany(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            name=company.name,
            domain=company.domain,
            website=company.website,
            description=company.description,
            industry=company.industry,
            company_type=company.company_type,
            lifecycle_stage=company.lifecycle_stage,
            owner_id=resolved_owner_id,
            number_of_employees=company.number_of_employees,
            annual_revenue=company.annual_revenue,
            phone=company.phone,
            city=company.city,
            state=company.state,
            country=company.country,
            address=company.address,
            postal_code=company.postal_code,
            timezone=company.timezone,
            linkedin_url=company.linkedin_url,
            is_active=True,
            source_created_at=company.source_created_at,
            source_modified_at=company.source_modified_at,
            synced_at=now,
            last_seen_at=now,
            properties=company.properties,
        )

    def _apply_updates(
        self,
        row: CRMCompany,
        company: NormalizedHubSpotCompany,
        resolved_owner_id: UUID | None,
        *,
        now: datetime,
    ) -> None:
        # company_id PK is referenced by CRMDeal.company_id / CRMContact.company_id
        # — do NOT rewrite it. owner_id is re-resolved every run so ownership
        # changes in HubSpot propagate.
        row.is_deleted = False
        row.name = company.name
        row.domain = company.domain
        row.website = company.website
        row.description = company.description
        row.industry = company.industry
        row.company_type = company.company_type
        row.lifecycle_stage = company.lifecycle_stage
        row.owner_id = resolved_owner_id
        row.number_of_employees = company.number_of_employees
        row.annual_revenue = company.annual_revenue
        row.phone = company.phone
        row.city = company.city
        row.state = company.state
        row.country = company.country
        row.address = company.address
        row.postal_code = company.postal_code
        row.timezone = company.timezone
        row.linkedin_url = company.linkedin_url
        row.source_created_at = company.source_created_at
        row.source_modified_at = company.source_modified_at
        row.synced_at = now
        row.last_seen_at = now
        row.properties = company.properties


def _has_same_or_newer_source_modified(
    row: CRMCompany,
    company: NormalizedHubSpotCompany,
) -> bool:
    """Return True when the active local row already has this provider state."""
    if (
        row.is_deleted
        or not row.is_active
        or row.source_modified_at is None
        or company.source_modified_at is None
    ):
        return False
    return _as_utc(row.source_modified_at) >= _as_utc(company.source_modified_at)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
