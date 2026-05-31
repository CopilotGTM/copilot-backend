"""Active-snapshot writer for CRM companies from Salesforce Account rows."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.companies.company_types import (
    CompanyWriteResult,
    NormalizedSalesforceCompany,
)

logger = get_logger(__name__)


class SalesforceCompanyWriter:
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
        normalized: list[NormalizedSalesforceCompany],
        all_external_ids: Iterable[str],
        owner_map: dict[str, UUID],
        deactivate_missing: bool = True,
    ) -> CompanyWriteResult:
        result = CompanyWriteResult()
        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows()

        for company in normalized:
            resolved_owner_id = self._resolve_owner_id(company.owner_external_id, owner_map, result)
            existing = existing_by_external.get(company.external_id)
            if existing is None:
                row = self._new_row(company, resolved_owner_id, now=now)
                self.session.add(row)
                result.companies_created += 1
            else:
                was_active = bool(existing.is_active)
                self._apply_updates(existing, company, resolved_owner_id, now=now)
                if was_active:
                    result.companies_updated += 1
                else:
                    existing.is_active = True
                    result.companies_reactivated += 1

        if deactivate_missing:
            snapshot_external_ids = set(all_external_ids)
            for external_id, row in existing_by_external.items():
                if external_id in snapshot_external_ids:
                    continue
                if row.is_active:
                    row.is_active = False
                    result.companies_deactivated += 1

        await self.session.flush()
        logger.info(
            "Salesforce company writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "companies_created": result.companies_created,
                "companies_updated": result.companies_updated,
                "companies_reactivated": result.companies_reactivated,
                "companies_deactivated": result.companies_deactivated,
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
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.integration_id) == self.integration_id,
            col(CRMCompany.is_deleted) == False,  # noqa: E712
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.external_id: row for row in rows if row.external_id is not None}

    def _new_row(
        self,
        company: NormalizedSalesforceCompany,
        resolved_owner_id: UUID | None,
        *,
        now: datetime,
    ) -> CRMCompany:
        return CRMCompany(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            external_id=company.external_id,
            name=company.name,
            domain=company.domain,
            website=company.website,
            description=company.description,
            industry=company.industry,
            company_type=company.company_type,
            lifecycle_stage=company.lifecycle_stage,
            owner_external_id=company.owner_external_id,
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
            data_payload=company.data_payload,
        )

    def _apply_updates(
        self,
        row: CRMCompany,
        company: NormalizedSalesforceCompany,
        resolved_owner_id: UUID | None,
        *,
        now: datetime,
    ) -> None:
        row.name = company.name
        row.domain = company.domain
        row.website = company.website
        row.description = company.description
        row.industry = company.industry
        row.company_type = company.company_type
        row.lifecycle_stage = company.lifecycle_stage
        row.owner_external_id = company.owner_external_id
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
        row.data_payload = company.data_payload
