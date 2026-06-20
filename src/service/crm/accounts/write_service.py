"""Write-side CRM account service.

Called by account API writes, the temporary access facade, and deal write flows
that need to materialize or copy account records. This module owns internal CRM
`CRMCompany` construction; callers own transaction commit/rollback so account
writes can participate in larger deal or API transactions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.crm.accounts.contracts import (
    AccountWriteContext,
    AccountWriteResult,
    AccountWriteServiceError,
)

_DOMAIN_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)
_COMPANY_NAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class AccountWriteService:
    """Canonical tenant-scoped account writer for internal CRM account records."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        """Bind writes to one org-scoped session without taking commit ownership."""
        self.session = session
        self.organisation_id = organisation_id

    @staticmethod
    def _clean_string(value: str | None) -> str | None:
        """Normalize optional user/provider text before duplicate comparisons."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _normalize_domain(domain: str | None) -> str | None:
        """Return the canonical domain key used for account duplicate checks."""
        if not domain or not domain.strip():
            return None
        d = domain.strip()
        d = _DOMAIN_PROTOCOL_RE.sub("", d)
        d = _DOMAIN_WWW_RE.sub("", d)
        d = d.split("/")[0].split("?")[0].split("#")[0]
        return d.lower() or None

    @staticmethod
    def normalize_company_name(value: str | None) -> str | None:
        """Return the normalized name key used for safe exact account matching."""
        cleaned = AccountWriteService._clean_string(value)
        if not cleaned:
            return None
        cleaned = cleaned.lower()
        cleaned = _COMPANY_NAME_NON_ALNUM_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split())
        return cleaned or None

    async def find_accounts_by_domain(self, domain: str) -> list[CRMCompany]:
        """Return active accounts whose normalized domain exactly matches."""
        normalized = self._normalize_domain(domain)
        if not normalized:
            return []
        c = cast(Any, CRMCompany).__table__.c
        # Duplicate checks must be tenant-scoped. External domains are natural
        # keys only inside an organisation, never globally.
        stmt = select(CRMCompany).where(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
            func.lower(c.domain) == normalized,
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def find_accounts_by_name(self, name: str) -> list[CRMCompany]:
        """Return active accounts whose name exactly matches case-insensitively."""
        cleaned = name.strip()
        if not cleaned:
            return []
        c = cast(Any, CRMCompany).__table__.c
        # Name-only creation is allowed only when there is a single exact tenant
        # match; otherwise the caller should surface the ambiguity.
        stmt = select(CRMCompany).where(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
            func.lower(c.name) == cleaned.lower(),
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def create_account(
        self,
        name: str | None,
        *,
        context: AccountWriteContext | None = None,
        **kwargs: Any,
    ) -> AccountWriteResult:
        """Create an internal CRM account after tenant-scoped duplicate checks."""
        cleaned_name = self._clean_string(name)
        raw_domain = kwargs.get("domain") or kwargs.get("website")
        normalized = self._normalize_domain(raw_domain)
        if not cleaned_name and not normalized:
            raise AccountWriteServiceError(
                "Account name, domain, or website is required",
                status_code=400,
            )

        if normalized:
            # Prefer domain duplicates because domains are the strongest user
            # supplied account key we have for manual account creation.
            existing = next(iter(await self.find_accounts_by_domain(normalized)), None)
            if existing:
                raise AccountWriteServiceError(
                    f"An account with domain '{normalized}' already exists",
                    status_code=409,
                    data={"company_id": str(existing.company_id)},
                )
        else:
            # Name-only accounts are valid in sales workflows, but exact
            # duplicate protection avoids obvious repeated manual rows.
            existing_by_name = next(
                iter(await self.find_accounts_by_name(cleaned_name or "")), None
            )
            if existing_by_name:
                raise AccountWriteServiceError(
                    f"An account named '{cleaned_name}' already exists",
                    status_code=409,
                    data={"company_id": str(existing_by_name.company_id)},
                )
        integration = await self._get_or_create_internal_integration(context)

        allowed_fields = {
            "domain",
            "website",
            "description",
            "industry",
            "company_type",
            "lifecycle_stage",
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
        # Ignore unknown fields at the service boundary so API/agent callers can
        # pass richer payloads without accidentally writing unsupported columns.
        company_kwargs: dict[str, Any] = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if normalized:
            company_kwargs["domain"] = normalized

        company = CRMCompany(
            company_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            name=cleaned_name or normalized,
            synced_at=datetime.now(UTC),
            is_deleted=False,
            **company_kwargs,
        )
        self.session.add(company)
        await self.session.flush()
        await self.session.refresh(company)
        return AccountWriteResult(account=company, created=True, matched_by="created")

    async def update_account(
        self,
        company_id: UUID,
        updates: dict[str, Any],
        *,
        context: AccountWriteContext | None = None,
    ) -> CRMCompany:
        """Update supported account fields on one tenant-owned active account."""
        _ = context
        c = cast(Any, CRMCompany).__table__.c
        # The company_id may come from a request or agent tool; organisation_id
        # is the security boundary that prevents cross-tenant updates.
        stmt = select(CRMCompany).where(
            c.company_id == company_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        res = await self.session.execute(stmt)
        company = res.scalars().first()
        if not company:
            raise AccountWriteServiceError("Account not found", status_code=404)

        domain_update_provided = "domain" in updates
        website_update_provided = "website" in updates
        candidate_domain = None
        if domain_update_provided:
            candidate_domain = self._normalize_domain(updates.get("domain"))
        elif website_update_provided:
            candidate_domain = self._normalize_domain(updates.get("website"))

        if candidate_domain:
            duplicate_stmt = select(CRMCompany).where(
                c.organisation_id == self.organisation_id,
                c.is_deleted.is_(False),
                func.lower(c.domain) == candidate_domain,
                c.company_id != company_id,
            )
            duplicate_res = await self.session.execute(duplicate_stmt)
            duplicate = duplicate_res.scalars().first()
            if duplicate:
                raise AccountWriteServiceError(
                    f"An account with domain '{candidate_domain}' already exists",
                    status_code=409,
                    data={"company_id": str(duplicate.company_id)},
                )

        allowed_fields = {
            "name",
            "domain",
            "website",
            "description",
            "industry",
            "company_type",
            "lifecycle_stage",
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
            # Unknown fields are intentionally ignored for compatibility with
            # older clients that may send presentation-only values.
            if field not in allowed_fields:
                continue
            if field == "domain":
                value = self._normalize_domain(value)
            setattr(company, field, value)

        if website_update_provided and not domain_update_provided and candidate_domain:
            company.domain = candidate_domain

        self.session.add(company)
        await self.session.flush()
        await self.session.refresh(company)
        return company

    async def upsert_company_from_account(
        self,
        account_payload: dict[str, Any],
        integration_id: UUID,
        now: datetime,
    ) -> tuple[CRMCompany | None, str | None]:
        """Resolve, copy, or create the account record requested by a deal write.

        Returns `(company, None)` for a resolved account, `(None, name)` when a
        name-only payload is ambiguous and should be kept as a deal snapshot, or
        `(None, None)` when the payload lacks enough account information.
        """
        explicit_company_id = account_payload.get("company_id")
        if explicit_company_id:
            if not isinstance(explicit_company_id, UUID):
                raise AccountWriteServiceError("Invalid account company_id", status_code=400)

            company = await self.load_org_company(explicit_company_id)
            # Deal writes can select an external-provider account. Copy it into
            # the internal CRM integration before participant links are created.
            return await self.ensure_internal_company_from_source(
                company, integration_id, now
            ), None

        website_provided = "website" in account_payload
        linkedin_provided = "linkedin_url" in account_payload
        industry_provided = "industry" in account_payload
        country_provided = "country" in account_payload
        name = self._clean_string(account_payload.get("name"))
        domain = self._normalize_domain(
            account_payload.get("domain") or account_payload.get("website")
        )
        website = self._clean_string(account_payload.get("website"))
        linkedin_url = self._clean_string(account_payload.get("linkedin_url"))
        industry = self._clean_string(account_payload.get("industry"))
        country = self._clean_string(account_payload.get("country"))
        normalized_name = self.normalize_company_name(name)

        if not domain:
            if not name:
                return None, None

            if normalized_name:
                # Name-only account payloads are risky. Reuse an existing record
                # only when the normalized name has exactly one internal match.
                stmt = select(CRMCompany).where(
                    col(CRMCompany.organisation_id) == self.organisation_id,
                    col(CRMCompany.integration_id) == integration_id,
                    col(CRMCompany.is_deleted).is_(False),
                    func.lower(func.trim(CRMCompany.name)) == normalized_name,
                )
                result = await self.session.execute(stmt)
                name_matches = list(result.scalars().all())

                if len(name_matches) == 1:
                    company = name_matches[0]
                    updated = False
                    if website_provided and company.website != website:
                        company.website = website
                        updated = True
                    if linkedin_provided and company.linkedin_url != linkedin_url:
                        company.linkedin_url = linkedin_url
                        updated = True
                    if industry_provided and company.industry != industry:
                        company.industry = industry
                        updated = True
                    if country_provided and company.country != country:
                        company.country = country
                        updated = True
                    if updated:
                        company.source_modified_at = now
                        company.synced_at = now
                        company.last_seen_at = now
                        self.session.add(company)
                    return company, None

                if len(name_matches) > 1:
                    # Multiple name matches are stored as a fallback snapshot on
                    # the deal so we do not choose the wrong account silently.
                    return None, name

            # No safe match exists, but the deal still needs a company entity for
            # participant links and future cleanup. Mark it provisional in JSONB.
            company = CRMCompany(
                company_id=uuid4(),
                organisation_id=self.organisation_id,
                integration_id=integration_id,
                name=name,
                domain=None,
                website=website,
                linkedin_url=linkedin_url,
                industry=industry,
                country=country,
                source_created_at=now,
                source_modified_at=now,
                synced_at=now,
                last_seen_at=now,
                properties={
                    "source": "internal_crm",
                    "created_via": "deal_create",
                    "is_provisional": True,
                    "match_source": "name_only_import",
                    "normalized_name": normalized_name or name.lower(),
                },
            )
            self.session.add(company)
            await self.session.flush()
            return company, None

        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.integration_id) == integration_id,
            col(CRMCompany.is_deleted).is_(False),
            func.lower(CRMCompany.domain) == domain.lower(),
        )
        result = await self.session.execute(stmt)
        matched_company = result.scalars().first()

        if matched_company:
            updated = False
            # User-provided deal account fields should repair stale company
            # snapshots, including older records whose name was set to a domain.
            if name and matched_company.name != name:
                matched_company.name = name
                updated = True
            if website_provided and matched_company.website != website:
                matched_company.website = website
                updated = True
            if linkedin_provided and matched_company.linkedin_url != linkedin_url:
                matched_company.linkedin_url = linkedin_url
                updated = True
            if industry_provided and matched_company.industry != industry:
                matched_company.industry = industry
                updated = True
            if country_provided and matched_company.country != country:
                matched_company.country = country
                updated = True

            if updated:
                matched_company.source_modified_at = now
                matched_company.synced_at = now
                matched_company.last_seen_at = now
                self.session.add(matched_company)
            return matched_company, None

        company = CRMCompany(
            company_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            name=name or domain,
            domain=domain,
            website=website,
            linkedin_url=linkedin_url,
            industry=industry,
            country=country,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={"source": "internal_crm", "created_via": "deal_create"},
        )
        self.session.add(company)
        await self.session.flush()
        return company, None

    async def load_org_company(self, company_id: UUID) -> CRMCompany:
        """Load one active account while enforcing the current organisation."""
        company = await self.session.get(CRMCompany, company_id)
        if company is None or company.organisation_id != self.organisation_id or company.is_deleted:
            raise AccountWriteServiceError("Account not found", status_code=404)
        return company

    async def ensure_internal_company_from_source(
        self,
        source_company: CRMCompany,
        integration_id: UUID,
        now: datetime,
    ) -> CRMCompany:
        """Return an internal CRM account equivalent to a selected source account."""
        if source_company.integration_id == integration_id:
            return source_company

        domain = self._normalize_domain(source_company.domain or source_company.website)
        if domain:
            # Domain is the preferred copy-dedupe key so reconnects and imports
            # reuse the same internal account instead of cloning provider rows.
            stmt = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.integration_id) == integration_id,
                col(CRMCompany.is_deleted).is_(False),
                func.lower(CRMCompany.domain) == domain.lower(),
            )
            result = await self.session.execute(stmt)
            matched_company = result.scalars().first()
            if matched_company:
                return matched_company

        normalized_name = self.normalize_company_name(source_company.name)
        if normalized_name:
            # If the source lacks a domain, use a single exact normalized-name
            # match. Ambiguous name matches fall through to a new copied account.
            stmt = select(CRMCompany).where(
                col(CRMCompany.organisation_id) == self.organisation_id,
                col(CRMCompany.integration_id) == integration_id,
                col(CRMCompany.is_deleted).is_(False),
                func.lower(func.trim(CRMCompany.name)) == normalized_name,
            )
            result = await self.session.execute(stmt)
            name_matches = list(result.scalars().all())
            if len(name_matches) == 1:
                return name_matches[0]

        company = CRMCompany(
            company_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            name=source_company.name,
            domain=domain,
            website=source_company.website,
            description=source_company.description,
            industry=source_company.industry,
            company_type=source_company.company_type,
            lifecycle_stage=source_company.lifecycle_stage,
            phone=source_company.phone,
            city=source_company.city,
            state=source_company.state,
            country=source_company.country,
            address=source_company.address,
            postal_code=source_company.postal_code,
            timezone=source_company.timezone,
            linkedin_url=source_company.linkedin_url,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                "source": "internal_crm",
                "created_via": "deal_create",
                "copied_from_company_id": str(source_company.company_id),
                "copied_from_integration_id": str(source_company.integration_id),
            },
        )
        self.session.add(company)
        await self.session.flush()
        return company

    async def get_account_audit_snapshot(self, company_id: UUID) -> dict[str, Any] | None:
        """Return the current account state for future mutation/audit event writers."""
        company = await self.load_org_company(company_id)
        return company.model_dump()

    async def _get_or_create_internal_integration(
        self, context: AccountWriteContext | None = None
    ) -> KnowledgeIntegration:
        """Return the internal CRM integration used to own manually created accounts."""
        _ = context
        ki = cast(Any, KnowledgeIntegration).__table__.c
        stmt = select(KnowledgeIntegration).where(
            ki.organisation_id == self.organisation_id,
            ki.integration_type == IntegrationType.INTERNAL_CRM.value,
        )
        res = await self.session.execute(stmt)
        integration = res.scalars().first()
        if integration:
            return integration

        # Preserve the historical automated sentinel for setup-created internal
        # integrations; the API actor is captured in AccountWriteContext for
        # later audit events, not as the integration creator.
        integration = KnowledgeIntegration(
            organisation_id=self.organisation_id,
            created_by_user_id=UUID("00000000-0000-0000-0000-000000000001"),
            created_by_admin=False,
            integration_type=IntegrationType.INTERNAL_CRM,
            integration_scope=IntegrationScope.ORGANIZATION,
            name="Copilot CRM",
            credentials={},
            connection_metadata={"source": "internal_crm", "internal": True},
            is_active=True,
        )
        self.session.add(integration)
        await self.session.flush()
        await self.session.refresh(integration)
        return integration
