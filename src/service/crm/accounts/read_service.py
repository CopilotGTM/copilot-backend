"""Read-only CRM account service.

Called by account API, agent tools, contact services, and the temporary
`AccountsAccessService` facade. This module owns account lookup/detail
projections only; account construction and mutation live in `AccountWriteService`
so reads cannot accidentally create CRM companies.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStatus,
    CRMParticipantType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.accounts.contracts import AccountReadServiceError

logger = get_logger("account_read_service")

_DOMAIN_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)
_CURSOR_NAME_KEY = "name"
_CURSOR_ID_KEY = "company_id"


def _encode_account_search_cursor(name_key: str | None, company_id: UUID) -> str:
    """Encode the account-search keyset cursor without exposing query internals."""
    payload = {
        _CURSOR_NAME_KEY: (name_key or "").strip().lower(),
        _CURSOR_ID_KEY: str(company_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_account_search_cursor(cursor: str) -> tuple[str, UUID]:
    """Decode the account-search cursor into `(lower_name, company_id)`."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        name_key = payload[_CURSOR_NAME_KEY]
        company_id = UUID(payload[_CURSOR_ID_KEY])
    except Exception as exc:
        raise AccountReadServiceError("Invalid cursor", status_code=400) from exc
    if not isinstance(name_key, str):
        raise AccountReadServiceError("Invalid cursor", status_code=400)
    return name_key, company_id


class AccountReadService:
    """Tenant-scoped account queries used by API and suggestion workflows."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        """Bind the service to one org-scoped session and immutable org boundary."""
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Close the owned org session when used as a FastAPI dependency."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("AccountReadService close exception", exc_info=True)

    @staticmethod
    def _normalize_domain(domain: str | None) -> str | None:
        """Return the canonical domain key used by exact-match account lookups."""
        if not domain or not domain.strip():
            return None
        d = domain.strip()
        d = _DOMAIN_PROTOCOL_RE.sub("", d)
        d = _DOMAIN_WWW_RE.sub("", d)
        d = d.split("/")[0].split("?")[0].split("#")[0]
        return d.lower() or None

    async def suggest_account_by_domain(self, email_domain: str) -> dict | None:
        """Return the lightweight dropdown match for one email domain."""
        normalized = email_domain.strip().lower()
        if not normalized:
            return None
        c = cast(Any, CRMCompany).__table__.c
        # The org filter is the trust boundary; caller-provided domains are not
        # enough to identify a safe account across tenant schemas.
        stmt = select(CRMCompany).where(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
            func.lower(c.domain) == normalized,
        )
        res = await self.org_session.execute(stmt)
        company = res.scalars().first()
        if not company:
            return None
        return {
            "company_id": company.company_id,
            "name": company.name,
            "domain": company.domain,
            "match_type": "domain_match",
        }

    async def find_accounts_by_domain(self, domain: str) -> list[CRMCompany]:
        """Return active accounts whose normalized domain exactly matches."""
        normalized = self._normalize_domain(domain)
        if not normalized:
            return []
        c = cast(Any, CRMCompany).__table__.c
        # Duplicate detection and suggestions both rely on this query staying
        # tenant-scoped; the same domain can legitimately exist in another org.
        stmt = select(CRMCompany).where(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
            func.lower(c.domain) == normalized,
        )
        res = await self.org_session.execute(stmt)
        return list(res.scalars().all())

    async def find_accounts_by_name(self, name: str) -> list[CRMCompany]:
        """Return active accounts whose name exactly matches case-insensitively."""
        cleaned = name.strip()
        if not cleaned:
            return []
        c = cast(Any, CRMCompany).__table__.c
        # Name-only matching is intentionally exact. Fuzzy matching here would
        # make duplicate prevention and deal materialization unpredictable.
        stmt = select(CRMCompany).where(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
            func.lower(c.name) == cleaned.lower(),
        )
        res = await self.org_session.execute(stmt)
        return list(res.scalars().all())

    async def list_accounts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        industry: str | None = None,
    ) -> tuple[list[dict], int]:
        """Return a paginated account list enriched for the accounts table UI."""
        if page < 1:
            raise AccountReadServiceError("Page must be >= 1", status_code=400)

        company_table = cast(Any, CRMCompany).__table__
        c = company_table.c
        co = cast(Any, CRMContact).__table__.c
        d = cast(Any, CRMDeal).__table__.c

        # Contacts can belong to an account either by explicit FK or by matching
        # the account domain; the UI expects both relationships in the count.
        contact_count_sub = (
            select(func.count(func.distinct(co.contact_id)).label("cnt"))
            .where(
                co.organisation_id == self.organisation_id,
                co.is_deleted.is_(False),
                or_(
                    co.company_id == c.company_id,
                    co.email.ilike(func.concat("%@", c.domain)),
                ),
            )
            .correlate(company_table)
            .scalar_subquery()
        )

        deal_count_sub = (
            select(func.count(d.deal_id))
            .where(
                d.organisation_id == self.organisation_id,
                d.is_deleted.is_(False),
                d.company_id == c.company_id,
            )
            .correlate(company_table)
            .scalar_subquery()
        )

        # Pipeline value is computed from open deals only, matching the account
        # summary metric shown in the app rather than total historical revenue.
        pipeline_value_sub = (
            select(func.coalesce(func.sum(d.amount), 0))
            .where(
                d.organisation_id == self.organisation_id,
                d.is_deleted.is_(False),
                d.company_id == c.company_id,
                d.deal_status == CRMDealStatus.OPEN.value,
            )
            .correlate(company_table)
            .scalar_subquery()
        )

        base_where = and_(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )

        extra_filters = []
        if q:
            like_expr = f"%{q.lower()}%"
            extra_filters.append(or_(c.name.ilike(like_expr), c.domain.ilike(like_expr)))
        if industry:
            extra_filters.append(c.industry.ilike(f"%{industry}%"))

        count_stmt = select(func.count(c.company_id)).where(base_where)
        for filter_expr in extra_filters:
            count_stmt = count_stmt.where(filter_expr)
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
        for filter_expr in extra_filters:
            stmt = stmt.where(filter_expr)

        res = await self.org_session.execute(stmt)

        result: list[dict] = []
        for row in res.all():
            company: CRMCompany = row[0]
            company_dict = company.model_dump()
            company_dict["owner"] = self._account_owner_payload(company)
            company_dict["contact_count"] = row[1] or 0
            company_dict["deal_count"] = row[2] or 0
            company_dict["pipeline_value"] = float(row[3] or 0)
            result.append(company_dict)

        return result, total_count

    @staticmethod
    def _account_owner_payload(company: CRMCompany) -> dict | None:
        """Build the nested owner object used by account list responses."""
        if not any((company.owner_id, company.owner_name, company.owner_email)):
            return None
        return {
            "id": company.owner_id,
            "name": company.owner_name,
            "email": company.owner_email,
        }

    async def search_accounts(
        self,
        *,
        q: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return lightweight account picker rows using keyset cursor pagination."""
        c = cast(Any, CRMCompany).__table__.c
        name_key = func.lower(func.coalesce(c.name, ""))

        filters = [
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        ]
        if q and q.strip():
            like_expr = f"%{q.strip().lower()}%"
            filters.append(
                or_(
                    c.name.ilike(like_expr),
                    c.domain.ilike(like_expr),
                    c.website.ilike(like_expr),
                )
            )

        if cursor:
            cursor_name, cursor_company_id = _decode_account_search_cursor(cursor)
            filters.append(
                or_(
                    name_key > cursor_name,
                    and_(name_key == cursor_name, c.company_id > cursor_company_id),
                )
            )

        stmt = (
            select(CRMCompany)
            .where(*filters)
            .order_by(name_key.asc(), c.company_id.asc())
            .limit(limit + 1)
        )
        res = await self.org_session.execute(stmt)
        rows = list(res.scalars().all())

        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_account_search_cursor(last.name, last.company_id)

        return [
            {
                "company_id": company.company_id,
                "name": company.name,
                "domain": company.domain,
                "website": company.website,
                "industry": company.industry,
            }
            for company in items
        ], next_cursor

    async def search_accounts_for_agent(
        self,
        *,
        q: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return fuzzy-ranked accounts for agent tool lookup.

        This keeps agent account search in the read service because it only
        reads existing account identity fields. Descriptive fields like industry
        and summaries stay out of matching to avoid noisy account resolution.
        """

        query = q.strip().lower() if q and q.strip() else None
        if not query:
            return []
        normalized_domain = self._normalize_domain(query)
        effective_limit = max(1, min(limit, 25))

        company_table = cast(Any, CRMCompany).__table__
        c = company_table.c
        co = cast(Any, CRMContact).__table__.c
        d = cast(Any, CRMDeal).__table__.c

        contact_count_sub = (
            select(func.count(func.distinct(co.contact_id)).label("cnt"))
            .where(
                co.organisation_id == self.organisation_id,
                co.is_deleted.is_(False),
                or_(
                    co.company_id == c.company_id,
                    co.email.ilike(func.concat("%@", c.domain)),
                ),
            )
            .correlate(company_table)
            .scalar_subquery()
        )
        deal_count_sub = (
            select(func.count(d.deal_id))
            .where(
                d.organisation_id == self.organisation_id,
                d.is_deleted.is_(False),
                d.company_id == c.company_id,
            )
            .correlate(company_table)
            .scalar_subquery()
        )

        name_score = func.similarity(func.coalesce(c.name, ""), query)
        domain_score = func.similarity(func.coalesce(c.domain, ""), normalized_domain or query)
        website_score = func.similarity(func.coalesce(c.website, ""), normalized_domain or query)
        exact_name = func.lower(func.coalesce(c.name, "")) == query
        exact_domain = func.lower(func.coalesce(c.domain, "")) == (normalized_domain or query)
        exact_website = func.lower(func.coalesce(c.website, "")) == query
        total_score = (
            case(
                (exact_domain, 2.0),
                (exact_name, 1.8),
                (exact_website, 1.2),
                else_=0.0,
            )
            + func.greatest(name_score, domain_score) * 1.4
            + website_score * 0.8
        )
        match_field = case(
            (exact_domain, "domain"),
            (exact_name, "name"),
            (exact_website, "website"),
            (domain_score >= func.greatest(name_score, website_score), "domain"),
            (name_score >= website_score, "name"),
            else_="website",
        )
        match_value = case(
            (match_field == "domain", c.domain),
            (match_field == "website", c.website),
            else_=c.name,
        )
        match_type = case(
            (or_(exact_domain, exact_name, exact_website), "exact"),
            else_="fuzzy",
        )

        stmt = (
            select(
                CRMCompany,
                contact_count_sub.label("contact_count"),
                deal_count_sub.label("deal_count"),
                total_score.label("score"),
                match_field.label("match_field"),
                match_value.label("match_value"),
                match_type.label("match_type"),
            )
            .where(
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
                or_(
                    c.name.ilike(f"%{query}%"),
                    c.domain.ilike(f"%{normalized_domain or query}%"),
                    c.website.ilike(f"%{query}%"),
                    c.name.op("%")(query),
                    c.domain.op("%")(normalized_domain or query),
                    c.website.op("%")(query),
                ),
            )
            .order_by(total_score.desc(), c.name.asc().nullslast(), c.company_id.asc())
            .limit(effective_limit)
        )
        res = await self.org_session.execute(stmt)
        return [
            {
                "company": row[0],
                "contact_count": row.contact_count or 0,
                "deal_count": row.deal_count or 0,
                "match": {
                    "field": row.match_field,
                    "value": row.match_value,
                    "type": row.match_type,
                    "score": float(row.score or 0.0),
                },
            }
            for row in res.all()
        ]

    async def get_account_detail(self, company_id: UUID) -> dict:
        """Return account detail with contacts, deals, and summary counters."""
        c = cast(Any, CRMCompany).__table__.c
        # Verify org ownership before loading child collections; a company_id
        # from another tenant must behave exactly like a missing account.
        stmt = select(CRMCompany).where(
            c.company_id == company_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        res = await self.org_session.execute(stmt)
        company = res.scalars().first()
        if not company:
            raise AccountReadServiceError("Account not found", status_code=404)

        contacts = await self.list_account_contacts(company_id)
        deals = await self.get_account_deals(company_id)

        contact_dicts = [
            {
                "contact_id": c["contact_id"],
                "full_name": c.get("full_name"),
                "email": c.get("email"),
                "job_title": c.get("job_title"),
            }
            for c in contacts
        ]

        detail = company.model_dump()
        detail["owner"] = self._account_owner_payload(company)
        detail["contacts"] = contact_dicts
        detail["deals"] = [dl.model_dump() for dl in deals]
        detail["contact_count"] = len(contacts)
        detail["deal_count"] = len(deals)
        detail["pipeline_value"] = float(
            sum((dl.amount or 0) for dl in deals if dl.deal_status == CRMDealStatus.OPEN.value)
        )
        detail["account_summary"] = self._account_summary_payload(company)
        return detail

    @staticmethod
    def _account_summary_payload(company: CRMCompany) -> dict[str, Any]:
        account_summary = getattr(company, "account_summary", None)
        last_researched_at = getattr(company, "last_researched_at", None)
        if isinstance(account_summary, str) and account_summary.strip():
            return {
                "status": "completed",
                "content": account_summary.strip(),
                "last_researched_at": last_researched_at,
            }
        if isinstance(last_researched_at, datetime):
            return {
                "status": "not_available",
                "content": None,
                "last_researched_at": last_researched_at,
            }
        return {
            "status": "not_available",
            "content": None,
            "last_researched_at": None,
        }

    async def list_account_contacts(
        self, company_id: UUID, *, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Return contact rows shaped for account detail and participant pickers."""
        contacts = await self.get_account_contacts(company_id, limit=limit)
        return [
            {
                "contact_id": row["contact"].contact_id,
                "full_name": row["contact"].full_name,
                "email": row["contact"].email,
                "job_title": row["contact"].job_title,
                "deal_count": row["deal_count"],
            }
            for row in contacts
        ]

    async def get_account_contacts(
        self, company_id: UUID, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return account contacts with per-contact deal participation counts."""
        c = cast(Any, CRMCompany).__table__.c
        # Resolve the account first so the domain fallback cannot be used without
        # also proving that the account belongs to this organisation.
        comp_stmt = select(CRMCompany).where(
            c.company_id == company_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        comp_res = await self.org_session.execute(comp_stmt)
        company = comp_res.scalars().first()
        if not company:
            return []

        co = cast(Any, CRMContact).__table__.c
        dp = cast(Any, CRMDealParticipant).__table__.c
        deal_count_expr = (
            select(func.count(func.distinct(dp.deal_id)))
            .where(
                dp.organisation_id == self.organisation_id,
                dp.is_deleted.is_(False),
                dp.participant_type == CRMParticipantType.CONTACT.value,
                dp.related_local_id == co.contact_id,
            )
            .correlate(cast(Any, CRMContact).__table__)
            .scalar_subquery()
        )
        conditions = [co.company_id == company_id]
        if company.domain:
            conditions.append(co.email.ilike(f"%@{company.domain}"))

        stmt = (
            select(CRMContact, deal_count_expr.label("deal_count"))
            .where(
                co.organisation_id == self.organisation_id,
                co.is_deleted.is_(False),
                or_(*conditions),
            )
            .order_by(
                deal_count_expr.desc(),
                func.lower(func.coalesce(co.full_name, co.email, "")).asc(),
                co.contact_id.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        res = await self.org_session.execute(stmt)
        return [{"contact": row[0], "deal_count": row[1] or 0} for row in res.all()]

    async def get_account_deals(self, company_id: UUID) -> list[CRMDeal]:
        """Return active deals linked to a tenant-owned account."""
        c = cast(Any, CRMCompany).__table__.c
        # Keep the ownership check separate from the deal query so a foreign
        # company_id cannot be used to infer whether another org has deals.
        comp_stmt = select(CRMCompany).where(
            c.company_id == company_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        comp_res = await self.org_session.execute(comp_stmt)
        company = comp_res.scalars().first()
        if not company:
            return []

        d = cast(Any, CRMDeal).__table__.c
        stmt = select(CRMDeal).where(
            d.organisation_id == self.organisation_id,
            d.is_deleted.is_(False),
            d.company_id == company.company_id,
        )
        res = await self.org_session.execute(stmt)
        return list(res.scalars().all())
