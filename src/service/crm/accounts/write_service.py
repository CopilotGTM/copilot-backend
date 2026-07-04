"""Write-side CRM account service.

Called by account API writes, the temporary access facade, and deal write flows
that need to materialize or copy account records. This module owns internal CRM
`CRMCompany` construction; callers own transaction commit/rollback so account
writes can participate in larger deal or API transactions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.crm.accounts.contracts import (
    AccountCreateCommand,
    AccountWriteContext,
    AccountWriteResult,
    AccountWriteServiceError,
)
from src.service.crm.accounts.domain_resolution_service import AccountDomainResolutionService
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.internal.internal_crm_setup_service import (
    AUTOMATED_PIPELINE_USER_ID,
    InternalCRMSetupService,
)
from src.service.crm.mutations.contracts import (
    CRMMutationContext,
    CRMMutationEventServiceError,
    CRMMutationRecordCommand,
)
from src.service.crm.mutations.event_service import CRMMutationEventService
from src.service.crm.mutations.policies import AccountMutationPolicy
from src.service.crm.utils.account import (
    ACCOUNT_LINKEDIN_URL_ERROR,
    clean_optional_string,
    is_account_linkedin_url,
    normalize_account_domain,
    normalize_company_name,
)
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)

AccountIdentityResolutionService = None


class AccountWriteService:
    """Canonical tenant-scoped account writer for internal CRM account records."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        master_session: AsyncSession | None = None,
    ):
        """Bind writes to one org-scoped session without taking commit ownership."""
        self.session = session
        self.organisation_id = organisation_id
        self.master_session = master_session
        self._mutation_events = CRMMutationEventService(session, organisation_id)
        self._projection = ActivityFeedProjectionService(session, organisation_id)

    async def find_accounts_by_domain(self, domain: str) -> list[CRMCompany]:
        """Return active accounts whose normalized domain exactly matches."""
        normalized = normalize_account_domain(domain)
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
        command: AccountCreateCommand,
        *,
        context: AccountWriteContext | None = None,
    ) -> AccountWriteResult:
        """Create or resolve an internal CRM account from web and LinkedIn identity."""
        linkedin_url = clean_optional_string(command.linkedin_url)
        if linkedin_url and not is_account_linkedin_url(linkedin_url):
            raise AccountWriteServiceError(ACCOUNT_LINKEDIN_URL_ERROR, status_code=400)

        if linkedin_url:
            resolver_cls = globals().get("AccountIdentityResolutionService")
            if resolver_cls is None:
                from src.service.crm.accounts.resolution_service import (
                    AccountIdentityResolutionService as ImportedAccountIdentityResolutionService,
                )
                from src.service.crm.accounts.resolution_service import (
                    AccountIdentityResolveCommand,
                )

                resolver_cls = ImportedAccountIdentityResolutionService
            else:
                from src.service.crm.accounts.resolution_service import (
                    AccountIdentityResolveCommand,
                )
            resolution = await resolver_cls(
                self.session,
                self.organisation_id,
                account_writer=self,
            ).resolve(
                AccountIdentityResolveCommand(
                    name=command.name,
                    domain=command.domain,
                    website=command.website,
                    linkedin_url=command.linkedin_url,
                    context=context,
                )
            )
            if resolution.status == "resolved" and resolution.account:
                return AccountWriteResult(
                    resolution.account,
                    resolution.created,
                    resolution.matched_by or "apollo",
                )
            if not any([command.name, command.domain, command.website]):
                raise AccountWriteServiceError(
                    resolution.error_detail or "Unable to resolve LinkedIn URL to an account",
                    status_code=404,
                )

        return await self._create_account_record(command, context=context)

    async def _create_account_record(
        self,
        command: AccountCreateCommand,
        *,
        context: AccountWriteContext | None = None,
    ) -> AccountWriteResult:
        """Create the CRMCompany row after identity resolution has already run."""
        cleaned_name = clean_optional_string(command.name)
        raw_domain = command.domain or command.website
        normalized = normalize_account_domain(raw_domain)
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
        integration = await InternalCRMSetupService(
            self.session,
            self.organisation_id,
            AUTOMATED_PIPELINE_USER_ID,
        ).get_or_create_internal_integration()
        owner_id = None
        if (
            context
            and context.actor_user_id
            and context.actor_user_id != AUTOMATED_PIPELINE_USER_ID
        ):
            owner_id = (await self._resolve_actor_crm_user(context.actor_user_id)).user_id

        company_kwargs: dict[str, Any] = {
            "domain": command.domain,
            "website": clean_optional_string(command.website),
            "industry": clean_optional_string(command.industry),
            "linkedin_url": clean_optional_string(command.linkedin_url),
        }
        company_kwargs = {key: value for key, value in company_kwargs.items() if value is not None}
        if normalized:
            company_kwargs["domain"] = normalized

        company = CRMCompany(
            company_id=uuid4(),
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            name=cleaned_name or normalized,
            owner_id=owner_id,
            owner_name=clean_optional_string(context.actor_name) if owner_id and context else None,
            owner_email=clean_optional_string(context.actor_email)
            if owner_id and context
            else None,
            synced_at=datetime.now(UTC),
            is_deleted=False,
            **company_kwargs,
        )
        self.session.add(company)
        await self.session.flush()
        await self.session.refresh(company)
        await self._projection.upsert_account_created(company)
        await self._record_account_create_event(company, context=context)
        return AccountWriteResult(account=company, created=True, matched_by="created")

    async def _resolve_actor_crm_user(self, actor_user_id: UUID) -> CRMUser:
        try:
            return await ensure_app_user_crm_identity(
                org_session=self.session,
                master_session=self.master_session,
                organisation_id=self.organisation_id,
                app_user_id=actor_user_id,
                actor_user_id=actor_user_id,
            )
        except CRMUserIdentityResolutionError as exc:
            raise AccountWriteServiceError(exc.message, status_code=exc.status_code) from exc

    async def resolve_or_create_account_for_contact_email(
        self,
        email: str,
        *,
        context: AccountWriteContext | None = None,
    ) -> CRMCompany | None:
        """Resolve one account by contact email domain, creating it when absent."""
        result = await self.resolve_or_create_account_for_contact_email_with_result(
            email,
            context=context,
        )
        return result.account if result else None

    async def resolve_or_create_account_for_contact_email_with_result(
        self,
        email: str,
        *,
        context: AccountWriteContext | None = None,
    ) -> AccountWriteResult | None:
        """Resolve one account by email domain and preserve whether it was created."""
        domain = AccountDomainResolutionService.derive_safe_domain_from_email(email)
        if not domain:
            return None

        matches = await self.find_accounts_by_domain(domain)
        if len(matches) == 1:
            return AccountWriteResult(account=matches[0], created=False, matched_by="domain")
        if len(matches) > 1:
            return None

        try:
            result = await self.create_account(
                AccountCreateCommand(name=domain, domain=domain),
                context=context,
            )
        except AccountWriteServiceError as exc:
            if exc.status_code == 409 and exc.data and exc.data.get("company_id"):
                retry_matches = await self.find_accounts_by_domain(domain)
                if len(retry_matches) == 1:
                    return AccountWriteResult(
                        account=retry_matches[0],
                        created=False,
                        matched_by="domain",
                    )
                return None
            raise
        return result

    async def update_account(
        self,
        company_id: UUID,
        updates: dict[str, Any],
        *,
        context: AccountWriteContext | None = None,
    ) -> CRMCompany:
        """Update supported account fields on one tenant-owned active account."""
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
            candidate_domain = normalize_account_domain(updates.get("domain"))
        elif website_update_provided:
            candidate_domain = normalize_account_domain(updates.get("website"))

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

        if linkedin_url := clean_optional_string(updates.get("linkedin_url")):
            if not is_account_linkedin_url(linkedin_url):
                raise AccountWriteServiceError(ACCOUNT_LINKEDIN_URL_ERROR, status_code=400)
            updates["linkedin_url"] = linkedin_url

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
        old_values = {
            field_name: getattr(company, field_name, None)
            for field_name in AccountMutationPolicy.UPDATE_AUDIT_FIELDS
        }

        for field, value in updates.items():
            # Unknown fields are intentionally ignored for compatibility with
            # older clients that may send presentation-only values.
            if field not in allowed_fields:
                continue
            if field == "domain":
                value = normalize_account_domain(value)
            setattr(company, field, value)

        if website_update_provided and not domain_update_provided and candidate_domain:
            company.domain = candidate_domain

        self.session.add(company)
        await self.session.flush()
        await self.session.refresh(company)
        changed_fields: dict[str, dict[str, Any]] = {}
        for field_name, old_value in old_values.items():
            if change := AccountMutationPolicy.changed_field(
                field_name, old_value, getattr(company, field_name, None)
            ):
                changed_fields[field_name] = change
        await self._record_account_update_event(
            company,
            changed_fields=changed_fields,
            context=context,
        )
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
        name = clean_optional_string(account_payload.get("name"))
        domain = normalize_account_domain(
            account_payload.get("domain") or account_payload.get("website")
        )
        website = clean_optional_string(account_payload.get("website"))
        linkedin_url = clean_optional_string(account_payload.get("linkedin_url"))
        industry = clean_optional_string(account_payload.get("industry"))
        country = clean_optional_string(account_payload.get("country"))
        normalized_name = normalize_company_name(name)

        if linkedin_url and not is_account_linkedin_url(linkedin_url):
            raise AccountWriteServiceError(ACCOUNT_LINKEDIN_URL_ERROR, status_code=400)

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

        domain = normalize_account_domain(source_company.domain or source_company.website)
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

        normalized_name = normalize_company_name(source_company.name)
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

    async def _record_account_create_event(
        self,
        account: CRMCompany,
        *,
        context: AccountWriteContext | None,
    ) -> None:
        changed_fields = AccountMutationPolicy.build_create_changed_fields(account)
        command = CRMMutationRecordCommand(
            entity_type="account",
            entity_id=account.company_id,
            event_type="crm.account.created",
            operation="create_account",
            changed_fields=changed_fields,
            semantic_event_types=AccountMutationPolicy.semantic_for_create(),
            context=self._mutation_context(context),
            summary=f"Account created: {account.name or account.domain or account.company_id}",
            integration_id=account.integration_id,
            idempotency_scope=f"account:create:{account.company_id}",
        )
        await self._record_mutation_event(command)

    async def _record_account_update_event(
        self,
        account: CRMCompany,
        *,
        changed_fields: dict[str, dict[str, Any]],
        context: AccountWriteContext | None,
    ) -> None:
        command = CRMMutationRecordCommand(
            entity_type="account",
            entity_id=account.company_id,
            event_type="crm.account.updated",
            operation="update_account",
            changed_fields=changed_fields,
            semantic_event_types=AccountMutationPolicy.semantic_for_update(changed_fields),
            context=self._mutation_context(context),
            summary=f"Account updated: {account.name or account.domain or account.company_id}",
            integration_id=account.integration_id,
            idempotency_scope=f"account:update:{account.company_id}",
        )
        await self._record_mutation_event(command)

    async def _record_mutation_event(self, command: CRMMutationRecordCommand) -> None:
        try:
            await self._mutation_events.record_event(command)
        except CRMMutationEventServiceError as exc:
            raise AccountWriteServiceError(
                exc.message,
                status_code=exc.status_code,
                data=exc.data,
            ) from exc

    @staticmethod
    def _mutation_context(context: AccountWriteContext | None) -> CRMMutationContext:
        if context is None:
            return CRMMutationContext(source="api")
        return CRMMutationContext(
            source=context.source,
            actor_user_id=context.actor_user_id,
            source_label=context.source_label,
        )
