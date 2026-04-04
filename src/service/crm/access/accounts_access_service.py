"""CRM service layer for accessing and managing account (company) data."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.utils.logger import get_logger

logger = get_logger("accounts_access_service")

# Strip protocol and optional www prefix, then capture up to the first path separator
_DOMAIN_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)


class AccountsAccessServiceError(Exception):
    """Custom exception for accounts access service errors."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class AccountsAccessService:
    """Service for accessing CRM accounts (companies) with filtering, pagination, and stats."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Initialize the service with an org-scoped session.

        Args:
            org_session: Async database session scoped to the organisation
            organisation_id: UUID of the organisation to scope queries
        """
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Clean up the session."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("AccountsAccessService close exception", exc_info=True)

    @staticmethod
    def _normalize_domain(domain: str | None) -> str | None:
        """Strip protocol, www prefix, path, and lowercase a domain string.

        Examples:
            "https://www.ACME.com/about" → "acme.com"
            "ACME.COM" → "acme.com"
            "" or None → None
        """
        if not domain or not domain.strip():
            return None
        d = domain.strip()
        d = _DOMAIN_PROTOCOL_RE.sub("", d)
        d = _DOMAIN_WWW_RE.sub("", d)
        # Drop any path, query string, or fragment
        d = d.split("/")[0].split("?")[0].split("#")[0]
        return d.lower() or None

    async def list_accounts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        industry: str | None = None,
    ) -> tuple[list[dict], int]:
        """Paginated accounts annotated with contact_count, deal_count, and pipeline_value.

        Returns:
            Tuple of (account dicts list, total_count). Each dict merges CRMCompany fields
            with contact_count, deal_count, and pipeline_value annotations.

        Raises:
            AccountsAccessServiceError: If page < 1.
        """
        if page < 1:
            raise AccountsAccessServiceError("Page must be >= 1", status_code=400)

        c = cast(Any, CRMCompany).__table__.c
        co = cast(Any, CRMContact).__table__.c
        d = cast(Any, CRMDeal).__table__.c

        # Count contacts per company by matching email domain to company.domain
        contact_count_sub = (
            select(
                func.count(co.contact_id).label("cnt"),
            )
            .where(
                co.organisation_id == self.organisation_id,
                co.is_deleted.is_(False),
                co.email.ilike(func.concat("%@", c.domain)),
            )
            .correlate(CRMCompany.__table__)
            .scalar_subquery()
        )

        # Count deals per company by company_external_id
        deal_count_sub = (
            select(func.count(d.deal_id))
            .where(
                d.organisation_id == self.organisation_id,
                d.is_deleted.is_(False),
                d.company_external_id == c.external_id,
            )
            .correlate(CRMCompany.__table__)
            .scalar_subquery()
        )

        # Sum pipeline value (open deals only)
        pipeline_value_sub = (
            select(func.coalesce(func.sum(d.amount), 0))
            .where(
                d.organisation_id == self.organisation_id,
                d.is_deleted.is_(False),
                d.company_external_id == c.external_id,
                d.deal_status == CRMDealStatus.OPEN.value,
            )
            .correlate(CRMCompany.__table__)
            .scalar_subquery()
        )

        base_where = and_(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )

        extra_filters = []
        if q:
            like_expr = f"%{q.lower()}%"
            extra_filters.append(
                or_(
                    c.name.ilike(like_expr),
                    c.domain.ilike(like_expr),
                )
            )
        if industry:
            extra_filters.append(c.industry.ilike(f"%{industry}%"))

        count_stmt = select(func.count(c.company_id)).where(base_where)
        for f in extra_filters:
            count_stmt = count_stmt.where(f)
        count_res = await self.org_session.execute(count_stmt)
        total_count: int = count_res.scalar() or 0

        stmt = (
            select(
                CRMCompany,
                contact_count_sub.label("contact_count"),
                deal_count_sub.label("deal_count"),
                pipeline_value_sub.label("pipeline_value"),
            )
            .where(base_where)
            .order_by(c.name.asc(), c.company_id.asc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        for f in extra_filters:
            stmt = stmt.where(f)

        res = await self.org_session.execute(stmt)
        rows = res.all()

        result: list[dict] = []
        for row in rows:
            company: CRMCompany = row[0]
            company_dict = company.model_dump()
            company_dict["contact_count"] = row[1] or 0
            company_dict["deal_count"] = row[2] or 0
            company_dict["pipeline_value"] = float(row[3] or 0)
            result.append(company_dict)

        return result, total_count

    async def get_account_detail(self, company_id: UUID) -> dict:
        """Full account detail with contacts list, deals list, and summary counts.

        Raises:
            AccountsAccessServiceError: 404 if not found.
        """
        c = cast(Any, CRMCompany).__table__.c
        stmt = select(CRMCompany).where(
            c.company_id == company_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        res = await self.org_session.execute(stmt)
        company = res.scalars().first()
        if not company:
            raise AccountsAccessServiceError("Account not found", status_code=404)

        contacts = await self.get_account_contacts(company_id)
        deals = await self.get_account_deals(company_id)

        # Batch-query deal counts per contact to avoid N+1 queries
        contact_ids = [c.contact_id for c in contacts]
        if contact_ids:
            count_stmt = (
                select(DealStakeholder.contact_id, func.count())
                .where(
                    col(DealStakeholder.contact_id).in_(contact_ids),
                    col(DealStakeholder.organisation_id) == self.organisation_id,
                    col(DealStakeholder.is_deleted).is_(False),
                )
                .group_by(DealStakeholder.contact_id)
            )
            count_res = await self.org_session.execute(count_stmt)
            deal_counts: dict = dict(count_res.all())
        else:
            deal_counts = {}

        contact_dicts = [
            {
                "contact_id": c.contact_id,
                "full_name": c.full_name,
                "email": c.email,
                "job_title": c.job_title,
                "deal_count": deal_counts.get(c.contact_id, 0),
            }
            for c in contacts
        ]

        detail = company.model_dump()
        detail["contacts"] = contact_dicts
        detail["deals"] = [dl.model_dump() for dl in deals]
        detail["contact_count"] = len(contacts)
        detail["deal_count"] = len(deals)
        # Pipeline value is sum of open deal amounts
        detail["pipeline_value"] = float(
            sum((dl.amount or 0) for dl in deals if dl.deal_status == CRMDealStatus.OPEN.value)
        )
        return detail

    async def get_account_contacts(self, company_id: UUID) -> list[CRMContact]:
        """Return contacts whose email domain matches the account's domain.

        Domain matching uses ILIKE '%@domain' so it's case-insensitive.
        """
        # Resolve company first to get its domain and verify org ownership
        c = cast(Any, CRMCompany).__table__.c
        comp_stmt = select(CRMCompany).where(
            c.company_id == company_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        comp_res = await self.org_session.execute(comp_stmt)
        company = comp_res.scalars().first()
        if not company or not company.domain:
            return []

        co = cast(Any, CRMContact).__table__.c
        stmt = select(CRMContact).where(
            co.organisation_id == self.organisation_id,
            co.is_deleted.is_(False),
            co.email.ilike(f"%@{company.domain}"),
        )
        res = await self.org_session.execute(stmt)
        return list(res.scalars().all())

    async def get_account_deals(self, company_id: UUID) -> list[CRMDeal]:
        """Return deals whose company_external_id matches the account's external_id."""
        # Resolve company first to get its external_id and verify org ownership
        c = cast(Any, CRMCompany).__table__.c
        comp_stmt = select(CRMCompany).where(
            c.company_id == company_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        comp_res = await self.org_session.execute(comp_stmt)
        company = comp_res.scalars().first()
        if not company or not company.external_id:
            return []

        d = cast(Any, CRMDeal).__table__.c
        stmt = select(CRMDeal).where(
            d.organisation_id == self.organisation_id,
            d.is_deleted.is_(False),
            d.company_external_id == company.external_id,
        )
        res = await self.org_session.execute(stmt)
        return list(res.scalars().all())

    async def create_account(self, name: str, **kwargs: Any) -> CRMCompany:
        """Create an internal CRM account.

        Checks for duplicate by normalized domain. Uses the INTERNAL_CRM integration
        as the integration owner, creating it if not yet set up.

        external_id is set to str(company_id), synced_at = now, is_deleted = False.

        Raises:
            AccountsAccessServiceError: 409 if a non-deleted account with the same domain
                already exists, or 409 if the internal CRM integration is not configured.
        """
        raw_domain = kwargs.get("domain")
        normalized = self._normalize_domain(raw_domain)

        if normalized:
            # Duplicate check by normalized domain
            c = cast(Any, CRMCompany).__table__.c
            dup_stmt = select(CRMCompany).where(
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
                func.lower(c.domain) == normalized,
            )
            dup_res = await self.org_session.execute(dup_stmt)
            existing = dup_res.scalars().first()
            if existing:
                raise AccountsAccessServiceError(
                    f"An account with domain '{normalized}' already exists",
                    status_code=409,
                    data={"company_id": str(existing.company_id)},
                )

        # Fetch internal CRM integration_id
        ki = cast(Any, KnowledgeIntegration).__table__.c
        int_stmt = select(KnowledgeIntegration).where(
            ki.organisation_id == self.organisation_id,
            ki.integration_type == IntegrationType.INTERNAL_CRM.value,
        )
        int_res = await self.org_session.execute(int_stmt)
        integration = int_res.scalars().first()
        if not integration:
            raise AccountsAccessServiceError(
                "Internal CRM integration not configured", status_code=409
            )

        company_id = uuid4()
        now = datetime.now(UTC)

        allowed_fields = {
            "domain",
            "website",
            "description",
            "industry",
            "company_type",
            "lifecycle_stage",
            "owner_external_id",
            "owner_email",
            "owner_name",
            "owner_id",
            "number_of_employees",
            "annual_revenue",
            "phone",
            "city",
            "state",
            "country",
            "address",
            "postal_code",
            "timezone",
            "linkedin_url",
        }
        company_kwargs: dict[str, Any] = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if normalized:
            company_kwargs["domain"] = normalized

        company = CRMCompany(
            company_id=company_id,
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            external_id=str(company_id),
            name=name,
            synced_at=now,
            is_deleted=False,
            **company_kwargs,
        )
        self.org_session.add(company)
        await self.org_session.flush()
        await self.org_session.refresh(company)
        return company

    async def update_account(self, company_id: UUID, updates: dict[str, Any]) -> CRMCompany:
        """Update allowed fields on an account. Normalizes domain if provided.

        Raises:
            AccountsAccessServiceError: 404 if not found.
        """
        c = cast(Any, CRMCompany).__table__.c
        stmt = select(CRMCompany).where(
            c.company_id == company_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        res = await self.org_session.execute(stmt)
        company = res.scalars().first()
        if not company:
            raise AccountsAccessServiceError("Account not found", status_code=404)

        allowed_fields = {
            "name",
            "domain",
            "website",
            "description",
            "industry",
            "company_type",
            "lifecycle_stage",
            "owner_external_id",
            "owner_email",
            "owner_name",
            "owner_id",
            "number_of_employees",
            "annual_revenue",
            "phone",
            "city",
            "state",
            "country",
            "address",
            "postal_code",
            "timezone",
            "linkedin_url",
        }

        for field, value in updates.items():
            if field not in allowed_fields:
                continue
            if field == "domain":
                value = self._normalize_domain(value)
            setattr(company, field, value)

        self.org_session.add(company)
        await self.org_session.flush()
        await self.org_session.refresh(company)
        return company
