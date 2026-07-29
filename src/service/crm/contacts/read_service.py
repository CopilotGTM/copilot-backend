"""Read-only CRM contact service."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import String, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import CRMAccessActor, DealAccessPolicy
from src.service.crm.accounts.domain_resolution_service import AccountDomainResolutionService
from src.service.crm.contacts.contracts import ContactReadServiceError
from src.service.crm.contacts.research_service import (
    CONTACT_RESEARCH_JOB_STALE_AFTER,
    PROFILE_METADATA_KEY,
    PROFILE_RAW_KEY,
)

logger = get_logger("contact_read_service")

_CURSOR_RANK_KEY = "rank"
_CURSOR_NAME_KEY = "name"
_CURSOR_ID_KEY = "contact_id"
MAX_AGENT_CONTACT_QUERY_CHARS = 200


def _escape_like(value: str) -> str:
    """Escape PostgreSQL LIKE metacharacters in agent-supplied identity text."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _encode_contact_search_cursor(rank: int, name_key: str | None, contact_id: UUID) -> str:
    payload = {
        _CURSOR_RANK_KEY: rank,
        _CURSOR_NAME_KEY: (name_key or "").strip().lower(),
        _CURSOR_ID_KEY: str(contact_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_contact_search_cursor(cursor: str) -> tuple[int, str, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        rank = int(payload[_CURSOR_RANK_KEY])
        name_key = payload[_CURSOR_NAME_KEY]
        contact_id = UUID(payload[_CURSOR_ID_KEY])
    except Exception as exc:
        raise ContactReadServiceError("Invalid cursor", status_code=400) from exc
    if not isinstance(name_key, str):
        raise ContactReadServiceError("Invalid cursor", status_code=400)
    return rank, name_key, contact_id


class ContactReadService:
    """Tenant-scoped contact queries used by API and agent workflows."""

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        access_actor: CRMAccessActor | None = None,
    ):
        """Bind the service to one org-scoped session and immutable org boundary."""
        self.org_session = org_session
        self.organisation_id = organisation_id
        self._deal_access = DealAccessPolicy(org_session, organisation_id, access_actor)

    async def close(self) -> None:
        """Close the owned org session when used as a FastAPI dependency."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("ContactReadService close exception", exc_info=True)

    @staticmethod
    def _extract_domain(email: str) -> str | None:
        """Extract lowered domain from email. Returns None if invalid."""
        if not email or "@" not in email:
            return None
        parts = email.rsplit("@", 1)
        domain = parts[1].strip().lower()
        return domain if domain else None

    @staticmethod
    def _map_source(integration_type: str | None) -> str:
        """Map an integration_type to the user-facing source label."""
        if not integration_type or integration_type == IntegrationType.INTERNAL_CRM.value:
            return "manual"
        return integration_type

    async def resolve_source_for_contact(self, integration_id: UUID) -> str:
        """Look up the integration type for a single contact and return its source label."""
        stmt = select(col(KnowledgeIntegration.integration_type)).where(
            col(KnowledgeIntegration.integration_id) == integration_id,
        )
        res = await self.org_session.execute(stmt)
        integration_type = res.scalar()
        return self._map_source(integration_type)

    async def list_contacts(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        q: str | None = None,
        company_id: UUID | None = None,
        sort_by: str | None = None,
        sort_order: str = "desc",
    ) -> tuple[list[dict], int]:
        """Paginated contacts with deal_count, source, company, and owner annotations."""
        if page < 1:
            raise ContactReadServiceError("Page must be >= 1", status_code=400)

        contact_participant_match = or_(
            col(CRMDealParticipant.related_local_id) == col(CRMContact.contact_id),
            func.lower(func.trim(col(CRMDealParticipant.related_email)))
            == func.lower(func.trim(col(CRMContact.email))),
        )

        deal_count_sub = (
            select(func.count(col(CRMDealParticipant.participant_id)))
            .where(
                contact_participant_match,
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted).is_(False),
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
            )
            .correlate(CRMContact)
            .scalar_subquery()
        )

        base = select(CRMContact, deal_count_sub.label("deal_count")).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )

        if q:
            like_expr = f"%{q.lower()}%"
            base = base.where(
                col(CRMContact.full_name).ilike(like_expr)
                | col(CRMContact.email).ilike(like_expr)
                | col(CRMContact.company_name).ilike(like_expr)
            )

        if company_id is not None:
            company_stmt = select(col(CRMCompany.domain)).where(
                col(CRMCompany.company_id) == company_id,
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            company_res = await self.org_session.execute(company_stmt)
            company_domain = company_res.scalar()
            if company_domain:
                base = base.where(
                    or_(
                        col(CRMContact.company_id) == company_id,
                        func.split_part(col(CRMContact.email), "@", 2).ilike(company_domain),
                    )
                )
            else:
                base = base.where(col(CRMContact.company_id) == company_id)

        count_stmt = select(func.count()).select_from(base.subquery())
        count_res = await self.org_session.execute(count_stmt)
        total_count = count_res.scalar() or 0

        c = cast(Any, CRMContact).__table__.c
        normalized_sort_order = sort_order.lower()
        if normalized_sort_order not in {"asc", "desc"}:
            raise ContactReadServiceError("sort_order must be asc or desc", status_code=400)

        sort_columns = {
            "full_name": func.lower(func.coalesce(c.full_name, c.email)),
            "owner": func.lower(func.coalesce(c.owner_name, c.owner_email)),
            "last_contacted_at": c.last_contacted_at,
            "last_activity_at": c.last_activity_at,
        }
        if sort_by is not None and sort_by not in sort_columns:
            raise ContactReadServiceError("Unsupported sort field", status_code=400)

        if sort_by is None:
            order_by = [c.created_at.desc(), c.contact_id.desc()]
        else:
            sort_column = sort_columns[sort_by]
            primary_order = (
                sort_column.asc().nullslast()
                if normalized_sort_order == "asc"
                else sort_column.desc().nullslast()
            )
            order_by = [primary_order, c.contact_id.asc()]

        stmt = (
            base.options(defer(cast(Any, CRMContact).profile_enrichment))
            .order_by(*order_by)
            .offset((page - 1) * limit)
            .limit(limit)
        )
        rows = (await self.org_session.execute(stmt)).all()

        integration_ids = {row.CRMContact.integration_id for row in rows}
        integration_map: dict[UUID, str] = {}
        if integration_ids:
            int_stmt = select(
                col(KnowledgeIntegration.integration_id),
                col(KnowledgeIntegration.integration_type),
            ).where(col(KnowledgeIntegration.integration_id).in_(list(integration_ids)))
            int_res = await self.org_session.execute(int_stmt)
            for int_id, int_type in int_res.all():
                integration_map[int_id] = int_type

        domains = {
            domain
            for row in rows
            if row.CRMContact.email
            for domain in [self._extract_domain(row.CRMContact.email)]
            if domain
        }
        company_ids = {row.CRMContact.company_id for row in rows if row.CRMContact.company_id}
        company_by_id: dict[UUID, CRMCompany] = {}
        company_by_domain: dict[str, CRMCompany] = {}
        if company_ids or domains:
            co_stmt = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
                or_(
                    col(CRMCompany.company_id).in_(list(company_ids)),
                    col(CRMCompany.domain).in_([d for d in domains if d]),
                ),
            )
            co_res = await self.org_session.execute(co_stmt)
            for matched_company in co_res.scalars().all():
                company_by_id[matched_company.company_id] = matched_company
                if matched_company.domain:
                    company_by_domain[matched_company.domain.lower()] = matched_company

        result: list[dict] = []
        for row in rows:
            contact: CRMContact = row.CRMContact
            email_domain = self._extract_domain(contact.email) if contact.email else None
            company: CRMCompany | None = None
            if contact.company_id:
                company = company_by_id.get(contact.company_id)
            elif email_domain:
                company = company_by_domain.get(email_domain)

            result.append(
                {
                    "contact": contact,
                    "deal_count": row.deal_count or 0,
                    "source": self._map_source(integration_map.get(contact.integration_id)),
                    "company": self._contact_company_payload(
                        contact=contact,
                        company=company,
                        email_domain=email_domain,
                    ),
                    "owner": self._contact_owner_payload(contact),
                    "company_id": company.company_id if company else contact.company_id,
                    "company_domain": email_domain,
                }
            )

        return result, total_count

    async def search_contacts(
        self,
        *,
        q: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        company_id: UUID | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return lightweight contact rows for picker and browse search."""
        return await self._search_contacts(
            q=q,
            limit=limit,
            cursor=cursor,
            company_id=company_id,
            include_company_name=True,
        )

    async def search_contacts_for_create(
        self,
        *,
        q: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return strict contact candidates used before creating a new contact."""
        return await self._search_contacts(
            q=q,
            limit=limit,
            cursor=cursor,
            company_id=None,
            include_company_name=False,
        )

    async def search_contacts_for_agent(
        self,
        *,
        q: str | None = None,
        company_name: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return fuzzy-ranked contacts for agent tool lookup.

        This is intentionally a read-service method, not a separate search
        subsystem: it only uses existing contact identity columns and the CRM
        trigram indexes. The agent gets match metadata so it can tell whether a
        person matched by name, email, alternate email, or company.
        """

        query = q.strip().lower() if q and q.strip() else None
        company_query = (
            company_name.strip().lower() if company_name and company_name.strip() else None
        )
        if not query and not company_query:
            return []
        if any(
            value is not None and len(value) > MAX_AGENT_CONTACT_QUERY_CHARS
            for value in (query, company_query)
        ):
            return []

        escaped_query = _escape_like(query) if query else None
        escaped_company_query = _escape_like(company_query) if company_query else None

        effective_limit = max(1, min(limit, 50))
        c = cast(Any, CRMContact).__table__.c
        additional_emails_text = c.additional_emails.cast(String)

        name_score = func.similarity(func.coalesce(c.full_name, ""), query or company_query)
        email_score = func.similarity(func.coalesce(c.email, ""), query or company_query)
        additional_email_score = func.similarity(
            func.coalesce(additional_emails_text, ""),
            query or company_query,
        )
        company_score = func.similarity(func.coalesce(c.company_name, ""), company_query or query)
        identity_score = func.greatest(name_score, email_score, additional_email_score)

        exact_email = func.lower(func.coalesce(c.email, "")) == (query or "")
        exact_name = func.lower(func.coalesce(c.full_name, "")) == (query or "")
        exact_additional_email = func.lower(func.coalesce(additional_emails_text, "")).like(
            f'%"{escaped_query}"%' if escaped_query else "__never_match__",
            escape="\\",
        )
        exact_company = func.lower(func.coalesce(c.company_name, "")) == (company_query or query)
        total_score = (
            case(
                (exact_email, 2.0),
                (exact_additional_email, 1.8),
                (exact_name, 1.6),
                (exact_company, 0.8),
                else_=0.0,
            )
            + identity_score * 1.4
            + company_score * 0.8
        )
        match_field = case(
            (exact_email, "email"),
            (exact_additional_email, "additional_emails"),
            (exact_name, "full_name"),
            (exact_company, "company_name"),
            (
                email_score >= func.greatest(name_score, additional_email_score, company_score),
                "email",
            ),
            (
                additional_email_score >= func.greatest(name_score, company_score),
                "additional_emails",
            ),
            (name_score >= company_score, "full_name"),
            else_="company_name",
        )
        match_value = case(
            (match_field == "email", c.email),
            (match_field == "additional_emails", additional_emails_text),
            (match_field == "company_name", c.company_name),
            else_=c.full_name,
        )
        match_type = case(
            (or_(exact_email, exact_additional_email, exact_name, exact_company), "exact"),
            else_="fuzzy",
        )

        filters = [
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        ]
        if query:
            filters.append(
                or_(
                    c.full_name.ilike(f"%{escaped_query}%", escape="\\"),
                    c.email.ilike(f"%{escaped_query}%", escape="\\"),
                    additional_emails_text.ilike(f"%{escaped_query}%", escape="\\"),
                    c.company_name.ilike(f"%{escaped_query}%", escape="\\"),
                    c.full_name.op("%")(query),
                    c.email.op("%")(query),
                    additional_emails_text.op("%")(query),
                    c.company_name.op("%")(query),
                )
            )
        if company_query:
            filters.append(
                or_(
                    c.company_name.ilike(f"%{escaped_company_query}%", escape="\\"),
                    c.company_name.op("%")(company_query),
                )
            )
        stmt = (
            select(
                CRMContact,
                total_score.label("score"),
                match_field.label("match_field"),
                match_value.label("match_value"),
                match_type.label("match_type"),
            )
            .options(defer(cast(Any, CRMContact).profile_enrichment))
            .where(*filters)
            .order_by(total_score.desc(), c.last_activity_at.desc().nullslast())
            .limit(effective_limit)
        )
        res = await self.org_session.execute(stmt)
        return [
            {
                "contact": row[0],
                "match": {
                    "field": row.match_field,
                    "value": row.match_value,
                    "type": row.match_type,
                    "score": float(row.score or 0.0),
                },
            }
            for row in res.all()
        ]

    async def _search_contacts(
        self,
        *,
        q: str | None,
        limit: int,
        cursor: str | None,
        company_id: UUID | None,
        include_company_name: bool,
    ) -> tuple[list[dict[str, Any]], str | None]:
        c = cast(Any, CRMContact).__table__.c
        query = q.strip().lower() if q and q.strip() else None
        name_key = func.lower(func.coalesce(c.full_name, c.email, ""))
        email_key = func.lower(func.coalesce(c.email, ""))
        company_key = func.lower(func.coalesce(c.company_name, ""))
        additional_emails_text = c.additional_emails.cast(String)
        additional_emails_key = func.lower(additional_emails_text)
        contact_participant_match = or_(
            col(CRMDealParticipant.related_local_id) == c.contact_id,
            func.lower(func.trim(col(CRMDealParticipant.related_email)))
            == func.lower(func.trim(c.email)),
        )
        deal_count_sub = (
            select(func.count(col(CRMDealParticipant.participant_id)))
            .where(
                contact_participant_match,
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted).is_(False),
                col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value,
            )
            .correlate(CRMContact)
            .scalar_subquery()
        )

        filters = [
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        ]
        if company_id is not None:
            filters.append(c.company_id == company_id)

        if query:
            like_expr = f"%{query}%"
            prefix_expr = f"{query}%"
            search_conditions = [
                email_key == query,
                c.email.ilike(prefix_expr),
                name_key == query,
                c.full_name.ilike(like_expr),
                additional_emails_text.ilike(like_expr),
            ]
            if include_company_name:
                search_conditions.append(c.company_name.ilike(like_expr))

            filters.append(or_(*search_conditions))
            rank_cases = [
                (email_key == query, 0),
                (additional_emails_key.like(f'%"{query}"%'), 1),
                (email_key.like(prefix_expr), 2),
                (name_key == query, 3),
                (name_key.like(prefix_expr), 4),
            ]
            if include_company_name:
                rank_cases.append((company_key.like(like_expr), 6))
            rank_expr = case(
                *rank_cases,
                else_=7,
            )
        else:
            rank_expr = case((c.contact_id.is_not(None), 0), else_=0)

        if cursor:
            cursor_rank, cursor_name, cursor_contact_id = _decode_contact_search_cursor(cursor)
            filters.append(
                or_(
                    rank_expr > cursor_rank,
                    and_(rank_expr == cursor_rank, name_key > cursor_name),
                    and_(
                        rank_expr == cursor_rank,
                        name_key == cursor_name,
                        c.contact_id > cursor_contact_id,
                    ),
                )
            )

        stmt = (
            select(CRMContact, deal_count_sub.label("deal_count"))
            .options(defer(cast(Any, CRMContact).profile_enrichment))
            .where(*filters)
            .order_by(rank_expr.asc(), name_key.asc(), c.contact_id.asc())
            .limit(limit + 1)
        )
        res = await self.org_session.execute(stmt)
        rows = list(res.all())

        has_more = len(rows) > limit
        contact_rows = rows[:limit]
        contacts = [row[0] for row in contact_rows]
        next_cursor = None
        if has_more and contacts:
            last = contacts[-1]
            next_cursor = _encode_contact_search_cursor(
                self._contact_match_rank(last, query, include_company_name),
                self._contact_name_key(last),
                last.contact_id,
            )

        companies = await self._load_companies_for_contacts(contacts)
        return [
            {
                "contact_id": contact.contact_id,
                "full_name": contact.full_name,
                "email": contact.email,
                "job_title": contact.job_title,
                "company": self._contact_company_payload(
                    contact=contact,
                    company=companies.get(contact.company_id) if contact.company_id else None,
                    email_domain=self._extract_domain(contact.email) if contact.email else None,
                ),
                "deal_count": contact_deal_count or 0,
                "match_type": self._contact_match_type(contact, query, include_company_name),
            }
            for contact, contact_deal_count in contact_rows
        ], next_cursor

    async def _load_companies_for_contacts(
        self, contacts: list[CRMContact]
    ) -> dict[UUID, CRMCompany]:
        company_ids = {contact.company_id for contact in contacts if contact.company_id}
        if not company_ids:
            return {}
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.is_deleted).is_(False),
            col(CRMCompany.company_id).in_(list(company_ids)),
        )
        res = await self.org_session.execute(stmt)
        return {company.company_id: company for company in res.scalars().all()}

    @staticmethod
    def _contact_name_key(contact: CRMContact) -> str:
        return (contact.full_name or contact.email or "").strip().lower()

    @classmethod
    def _contact_match_rank(
        cls, contact: CRMContact, query: str | None, include_company_name: bool
    ) -> int:
        match_type = cls._contact_match_type(contact, query, include_company_name)
        return {
            "exact_email": 0,
            "additional_email": 1,
            "email_prefix": 2,
            "exact_name": 3,
            "name_prefix": 4,
            "name": 5,
            "company": 6,
            "other": 7,
        }[match_type]

    @staticmethod
    def _contact_match_type(
        contact: CRMContact, query: str | None, include_company_name: bool
    ) -> str:
        if not query:
            return "other"
        email = (contact.email or "").strip().lower()
        name = (contact.full_name or "").strip().lower()
        company = (contact.company_name or "").strip().lower()
        additional_emails = [
            value.strip().lower() for value in contact.additional_emails or [] if value.strip()
        ]
        if email == query:
            return "exact_email"
        if query in additional_emails:
            return "additional_email"
        if email.startswith(query):
            return "email_prefix"
        if name == query:
            return "exact_name"
        if name.startswith(query):
            return "name_prefix"
        if query in name:
            return "name"
        if include_company_name and query in company:
            return "company"
        return "other"

    @staticmethod
    def _contact_company_payload(
        *,
        contact: CRMContact,
        company: CRMCompany | None,
        email_domain: str | None,
    ) -> dict | None:
        """Build the nested company object used by list responses."""
        company_id = company.company_id if company else contact.company_id
        name = company.name if company else contact.company_name
        domain = company.domain if company else email_domain
        if not any((company_id, name, domain)):
            return None
        return {"id": company_id, "name": name, "domain": domain}

    @staticmethod
    def _contact_owner_payload(contact: CRMContact) -> dict | None:
        """Build the nested owner object used by list responses."""
        if not any((contact.owner_id, contact.owner_name, contact.owner_email)):
            return None
        return {
            "id": contact.owner_id,
            "name": contact.owner_name,
            "email": contact.owner_email,
        }

    async def get_contact_detail(self, contact_id: UUID) -> dict:
        """Full contact + deal involvement via contact participants."""
        profile_column = cast(Any, CRMContact).__table__.c.profile_enrichment
        public_profile = profile_column.op("-")(PROFILE_RAW_KEY).op("-")(PROFILE_METADATA_KEY)
        # Fetch the public JSONB projection separately and defer the mapped field.
        # This keeps the full Apollo object out of routine contact-detail memory.
        stmt = (
            select(CRMContact, public_profile.label("public_profile_enrichment"))
            .options(defer(cast(Any, CRMContact).profile_enrichment))
            .where(
                col(CRMContact.contact_id) == contact_id,
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
            )
        )
        res = await self.org_session.execute(stmt)
        row = res.first()
        if row is None:
            raise ContactReadServiceError("Contact not found", status_code=404)
        contact = row[0]

        participant_contact_match = col(CRMDealParticipant.related_local_id) == contact_id
        if contact.email:
            participant_contact_match = or_(
                participant_contact_match,
                func.lower(func.trim(col(CRMDealParticipant.related_email)))
                == contact.email.strip().lower(),
            )

        deal_stmt = (
            select(CRMDeal, CRMDealParticipant)
            .join(
                CRMDealParticipant,
                (col(CRMDealParticipant.deal_id) == col(CRMDeal.deal_id))
                & participant_contact_match
                & (col(CRMDealParticipant.organisation_id) == self.organisation_id)
                & (col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value)
                & (col(CRMDealParticipant.is_deleted).is_(False))
                & (col(CRMDealParticipant.approval_status) == ApprovalStatus.APPROVED.value),
            )
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.is_deleted).is_(False),
                self._deal_access.visible_deal_predicate(cast(Any, CRMDeal).__table__.c),
            )
        )
        deal_res = await self.org_session.execute(deal_stmt)
        deals = [
            {
                "deal": row.CRMDeal,
                "participant": row.CRMDealParticipant,
            }
            for row in deal_res.all()
        ]

        resolved_company_id = contact.company_id
        email_domain = self._extract_domain(contact.email) if contact.email else None
        company: CRMCompany | None = None
        if resolved_company_id:
            company_stmt = select(CRMCompany).where(
                col(CRMCompany.company_id) == resolved_company_id,
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
            )
            company_res = await self.org_session.execute(company_stmt)
            company = company_res.scalars().first()
        if not resolved_company_id and email_domain:
            company_match_stmt = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.is_deleted).is_(False),
                col(CRMCompany.domain) == email_domain,
            )
            company_match_res = await self.org_session.execute(company_match_stmt)
            company = company_match_res.scalars().first()
            if company:
                resolved_company_id = company.company_id

        return {
            "contact": contact,
            "profile_enrichment": row.public_profile_enrichment or {},
            "research_status": await self._contact_research_status(
                contact,
                row.public_profile_enrichment or {},
            ),
            "deals": deals,
            "company": self._contact_detail_company_payload(
                contact=contact,
                company=company,
                email_domain=email_domain,
            ),
            "owner": self._contact_owner_payload(contact),
            "source": await self.resolve_source_for_contact(contact.integration_id),
        }

    async def _contact_research_status(
        self,
        contact: CRMContact,
        profile_enrichment: dict[str, Any],
    ) -> str:
        """Return the polling state for the contact overview's research panel."""
        if contact.contact_research:
            return "completed"

        email = (contact.email or "").strip().lower()
        linkedin_url = (contact.linkedin_url or "").strip() or None

        # copilotgtm: contact_id still lives in JSONB. Keep the lookup bounded in SQL;
        # promote it to an indexed column if contact-job volume makes this expression hot.
        stmt = (
            select(DealJobs)
            .where(
                col(DealJobs.organisation_id) == self.organisation_id,
                col(DealJobs.job_type) == DealJobType.CONTACT_RESEARCH,
                DealJobs.job_metadata["contact_id"].as_string() == str(contact.contact_id),
            )
            .order_by(col(DealJobs.created_at).desc())
            .limit(1)
        )
        result = await self.org_session.execute(stmt)
        job = result.scalars().first()
        if job:
            metadata = job.job_metadata or {}
            if (
                metadata.get("normalized_email") != (email or None)
                or metadata.get("linkedin_url") != linkedin_url
            ):
                return "not_started"
            status = getattr(job.status, "value", job.status)
            if status in {JobStatus.PENDING.value, JobStatus.IN_PROGRESS.value}:
                job_updated_at = job.updated_at or job.created_at
                if job_updated_at and job_updated_at < datetime.now(UTC) - (
                    CONTACT_RESEARCH_JOB_STALE_AFTER
                ):
                    return "not_started"
                return str(status)
            if status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
                return "failed"
            result_data = job.result_data or {}
            if result_data.get("provider_status") in {"not_found", "skipped"}:
                return "not_found"
            if result_data.get("summary_status") in {"error", "skipped"}:
                return "failed"
            if status == JobStatus.COMPLETED.value:
                # A completed job without stored research is inconsistent but terminal;
                # polling it forever cannot repair the missing write.
                return "failed"

        if profile_enrichment.get("status") == "not_found":
            return "not_found"
        has_safe_email = bool(AccountDomainResolutionService.derive_safe_domain_from_email(email))
        has_provider_data = bool(profile_enrichment)
        if contact.linkedin_url or has_safe_email or has_provider_data:
            return "not_started"
        return "not_eligible"

    @staticmethod
    def _contact_detail_company_payload(
        *,
        contact: CRMContact,
        company: CRMCompany | None,
        email_domain: str | None,
    ) -> dict | None:
        """Build the account object used by the contact detail response."""
        company_id = company.company_id if company else contact.company_id
        name = company.name if company else contact.company_name
        domain = company.domain if company else email_domain
        if not any((company_id, name, domain)):
            return None
        return {
            "id": company_id,
            "name": name,
            "domain": domain,
            "website": company.website if company else None,
            "industry": company.industry if company else None,
            "linkedin_url": company.linkedin_url if company else None,
            "country": company.country if company else None,
        }

    async def find_contact_by_email(self, email: str) -> CRMContact | None:
        """Return a non-deleted contact by exact case-insensitive email."""
        normalized_email = email.strip().lower()
        if not normalized_email:
            return None
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(col(CRMContact.email)) == normalized_email,
        )
        res = await self.org_session.execute(stmt)
        return res.scalars().first()
