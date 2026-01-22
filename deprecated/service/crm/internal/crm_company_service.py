"""Service for internal CRM company CRUD operations."""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealParticipant, CRMParticipantType
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.internal.internal_crm_user_service import InternalCRMUserService

logger = get_logger(__name__)

DOMAIN_CLEAN_RE = re.compile(r"^https?://", re.IGNORECASE)


class CRMCompanyServiceError(Exception):
    """Custom exception for CRM company service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class CRMCompanyService:
    """Service for managing internal CRM companies."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, actor_user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("CRMCompanyService close exception", exc_info=True)

    async def list_companies(
        self, *, limit: int = 50, cursor: str | None = None, q: str | None = None
    ) -> tuple[list[CRMCompany], str | None]:
        integration_id = await self._get_internal_integration_id(require=False)
        if not integration_id:
            return [], None

        c = cast(Any, CRMCompany).__table__.c
        stmt = select(CRMCompany).where(
            c.organisation_id == self.organisation_id,
            c.integration_id == integration_id,
            c.is_deleted.is_(False),
        )

        if q:
            search = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    c.name.ilike(search),
                    c.domain.ilike(search),
                    c.industry.ilike(search),
                )
            )

        if cursor:
            try:
                ts, company_id = decode_cursor(cursor)
            except Exception as exc:
                raise CRMCompanyServiceError("Invalid cursor", status_code=400) from exc

            stmt = stmt.where(
                or_(
                    c.created_at < ts,
                    and_(c.created_at == ts, c.company_id < company_id),
                )
            )

        stmt = stmt.order_by(c.created_at.desc(), c.company_id.desc()).limit(limit + 1)
        result = await self.session.execute(stmt)
        rows = list(result.scalars().all())

        has_more = len(rows) == limit + 1
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].company_id)
            if has_more and items
            else None
        )
        return items, next_cursor

    async def create_company(self, payload: dict[str, Any]) -> CRMCompany:
        integration_id = await self._get_internal_integration_id(require=True)
        now = datetime.now(UTC)

        company_data = self._normalize_payload(payload)
        domain = self._normalize_domain(company_data.get("domain"))
        company_data["domain"] = domain
        if company_data.get("is_active") is None:
            company_data.pop("is_active", None)

        await self._ensure_company_unique(integration_id, domain, company_data.get("name"))

        owner = None
        owner_user_id = company_data.get("owner_user_id")
        if owner_user_id:
            owner_service = InternalCRMUserService(
                self.session, self.organisation_id, self.actor_user_id
            )
            owner = await owner_service.ensure_crm_user(owner_user_id)

        company_id = uuid4()
        properties = dict(company_data.get("properties") or {})
        properties["source"] = "internal_crm"

        company = CRMCompany(
            company_id=company_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            external_id=str(company_id),
            name=company_data.get("name"),
            domain=domain,
            website=company_data.get("website"),
            description=company_data.get("description"),
            industry=company_data.get("industry"),
            company_type=company_data.get("company_type"),
            lifecycle_stage=company_data.get("lifecycle_stage"),
            number_of_employees=company_data.get("number_of_employees"),
            annual_revenue=company_data.get("annual_revenue"),
            phone=company_data.get("phone"),
            city=company_data.get("city"),
            state=company_data.get("state"),
            country=company_data.get("country"),
            address=company_data.get("address"),
            postal_code=company_data.get("postal_code"),
            timezone=company_data.get("timezone"),
            linkedin_url=company_data.get("linkedin_url"),
            is_active=company_data.get("is_active", True),
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties=properties,
        )

        if owner:
            company.owner_id = owner.user_id
            company.owner_external_id = owner.external_id
            company.owner_name = owner.full_name
            company.owner_email = owner.email

        self.session.add(company)
        return company

    async def update_company(
        self, company_id: UUID, payload: dict[str, Any]
    ) -> CRMCompany:
        integration_id = await self._get_internal_integration_id(require=True)
        company = await self._get_internal_company(company_id, integration_id)

        company_data = self._normalize_payload(payload)
        domain = company_data.get("domain")
        if "is_active" in company_data and company_data.get("is_active") is None:
            company_data.pop("is_active", None)
        if "domain" in company_data:
            domain = self._normalize_domain(domain)
            company_data["domain"] = domain

        name = company_data.get("name") if "name" in company_data else company.name
        if "domain" in company_data or "name" in company_data:
            await self._ensure_company_unique(
                integration_id,
                domain if "domain" in company_data else company.domain,
                name,
                exclude=company_id,
            )

        for field in [
            "name",
            "domain",
            "website",
            "description",
            "industry",
            "company_type",
            "lifecycle_stage",
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
            "is_active",
        ]:
            if field in company_data:
                setattr(company, field, company_data.get(field))

        if "owner_user_id" in company_data:
            owner_user_id = company_data.get("owner_user_id")
            if owner_user_id:
                owner_service = InternalCRMUserService(
                    self.session, self.organisation_id, self.actor_user_id
                )
                owner = await owner_service.ensure_crm_user(owner_user_id)
                company.owner_id = owner.user_id
                company.owner_external_id = owner.external_id
                company.owner_name = owner.full_name
                company.owner_email = owner.email
            else:
                company.owner_id = None
                company.owner_external_id = None
                company.owner_name = None
                company.owner_email = None

        if "properties" in company_data:
            properties = dict(company.properties or {})
            properties.update(company_data.get("properties") or {})
            properties["source"] = "internal_crm"
            company.properties = properties

        now = datetime.now(UTC)
        company.source_modified_at = now
        company.synced_at = now
        company.last_seen_at = now
        self.session.add(company)

        await self._sync_company_references(
            integration_id=integration_id,
            company=company,
            updated_at=now,
        )

        return company

    async def delete_company(self, company_id: UUID) -> CRMCompany:
        integration_id = await self._get_internal_integration_id(require=True)
        company = await self._get_internal_company(company_id, integration_id)

        now = datetime.now(UTC)
        company.is_deleted = True
        company.source_modified_at = now
        company.synced_at = now
        company.last_seen_at = now
        self.session.add(company)

        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == integration_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.COMPANY.value,
            col(CRMDealParticipant.related_local_id) == company_id,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        participants = list(result.scalars().all())

        for participant in participants:
            participant.is_deleted = True
            participant.source_modified_at = now
            participant.synced_at = now
            participant.last_seen_at = now
            self.session.add(participant)

        await self._clear_company_from_deals(integration_id, company, now)
        return company

    async def _get_internal_integration_id(self, *, require: bool) -> UUID | None:
        setup_service = InternalCRMSetupService(
            self.session, self.organisation_id, self.actor_user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration:
            if require:
                raise CRMCompanyServiceError(
                    "Internal CRM is not configured", status_code=409
                )
            return None
        return integration.integration_id

    async def _get_internal_company(self, company_id: UUID, integration_id: UUID) -> CRMCompany:
        company = await self.session.get(CRMCompany, company_id)
        if not company or company.organisation_id != self.organisation_id:
            raise CRMCompanyServiceError("Company not found", status_code=404)
        if company.integration_id != integration_id:
            raise CRMCompanyServiceError("External CRM companies are read-only", status_code=409)
        if company.is_deleted:
            raise CRMCompanyServiceError("Company is deleted", status_code=404)
        return company

    async def _ensure_company_unique(
        self,
        integration_id: UUID,
        domain: str | None,
        name: str | None,
        *,
        exclude: UUID | None = None,
    ) -> None:
        c = cast(Any, CRMCompany).__table__.c
        stmt = select(CRMCompany).where(
            c.organisation_id == self.organisation_id,
            c.integration_id == integration_id,
            c.is_deleted.is_(False),
        )
        if domain:
            stmt = stmt.where(func.lower(c.domain) == domain.lower())
        elif name:
            stmt = stmt.where(func.lower(c.name) == name.lower())
        else:
            return

        if exclude:
            stmt = stmt.where(c.company_id != exclude)

        result = await self.session.execute(stmt)
        existing = result.scalars().first()
        if existing:
            raise CRMCompanyServiceError("Company already exists", status_code=409)

    async def _sync_company_references(
        self,
        *,
        integration_id: UUID,
        company: CRMCompany,
        updated_at: datetime,
    ) -> None:
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == integration_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.COMPANY.value,
            col(CRMDealParticipant.related_local_id) == company.company_id,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        participants = list(result.scalars().all())

        for participant in participants:
            participant.related_name = company.name
            participant.source_modified_at = updated_at
            participant.synced_at = updated_at
            participant.last_seen_at = updated_at
            self.session.add(participant)

        deal_stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.company_external_id) == company.external_id,
            col(CRMDeal.is_deleted).is_(False),
        )
        deal_result = await self.session.execute(deal_stmt)
        deals = list(deal_result.scalars().all())

        for deal in deals:
            deal.company_name = company.name
            deal.company_domain = company.domain
            deal.source_modified_at = updated_at
            deal.synced_at = updated_at
            deal.last_seen_at = updated_at
            self.session.add(deal)

    async def _clear_company_from_deals(
        self, integration_id: UUID, company: CRMCompany, updated_at: datetime
    ) -> None:
        stmt = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.company_external_id) == company.external_id,
            col(CRMDeal.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        deals = list(result.scalars().all())

        for deal in deals:
            deal.company_external_id = None
            deal.company_name = None
            deal.company_domain = None
            deal.source_modified_at = updated_at
            deal.synced_at = updated_at
            deal.last_seen_at = updated_at
            self.session.add(deal)

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, str):
                cleaned[key] = self._clean_string(value)
            else:
                cleaned[key] = value
        return cleaned

    @staticmethod
    def _clean_string(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def _normalize_domain(self, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = self._clean_string(value)
        if not cleaned:
            return None
        cleaned = DOMAIN_CLEAN_RE.sub("", cleaned)
        cleaned = cleaned.split("/")[0]
        cleaned = cleaned.lower()
        if cleaned.startswith("www."):
            cleaned = cleaned[4:]
        return cleaned or None
