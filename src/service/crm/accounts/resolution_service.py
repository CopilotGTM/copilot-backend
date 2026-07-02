"""Account identity resolution for LinkedIn-first account enrichment flows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.service.crm.accounts.contracts import (
    AccountCreateCommand,
    AccountWriteContext,
    AccountWriteResult,
)
from src.service.crm.accounts.research_service import (
    AccountProviderResearchInput,
    AccountProviderResearchResult,
    AccountResearchService,
    research_account_with_apollo,
)
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.utils.account import clean_optional_string, normalize_account_domain

AccountIdentityResolutionStatus = Literal["resolved", "not_found", "error"]
AccountIdentityMatchedBy = Literal["linkedin", "domain", "apollo", "created"]
AccountIdentityProviderLookup = Callable[
    [AccountProviderResearchInput], Awaitable[AccountProviderResearchResult]
]


@dataclass(frozen=True)
class AccountIdentityResolveCommand:
    """Identity inputs for resolving one CRMCompany before another flow depends on it."""

    linkedin_url: str | None = None
    domain: str | None = None
    website: str | None = None
    name: str | None = None
    context: AccountWriteContext | None = None


@dataclass(frozen=True)
class AccountIdentityResolutionResult:
    """Resolved account or typed miss returned to future account/stakeholder callers."""

    status: AccountIdentityResolutionStatus
    account: CRMCompany | None = None
    created: bool = False
    matched_by: AccountIdentityMatchedBy | None = None
    error_detail: str | None = None


class AccountIdentityResolutionService:
    """Resolve accounts by existing CRM identity first, then Apollo organization data."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        account_writer: AccountWriteService | None = None,
        provider_lookup: AccountIdentityProviderLookup = research_account_with_apollo,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.account_writer = account_writer or AccountWriteService(session, organisation_id)
        self.provider_lookup = provider_lookup

    async def resolve(
        self,
        command: AccountIdentityResolveCommand,
    ) -> AccountIdentityResolutionResult:
        """Resolve an existing account or create one from usable Apollo organization data."""
        linkedin_url = clean_optional_string(command.linkedin_url)
        domain = normalize_account_domain(command.domain or command.website)
        website = clean_optional_string(command.website)
        name = clean_optional_string(command.name)

        if linkedin_url and (account := await self._find_account_by_linkedin_url(linkedin_url)):
            return AccountIdentityResolutionResult(
                status="resolved",
                account=account,
                matched_by="linkedin",
            )

        if domain and (account := await self._find_account_by_domain(domain)):
            return AccountIdentityResolutionResult(
                status="resolved",
                account=account,
                matched_by="domain",
            )

        if not any([linkedin_url, domain, website, name]):
            return AccountIdentityResolutionResult(
                status="not_found",
                error_detail="LinkedIn URL, domain, website, or name is required",
            )

        provider_result = await self.provider_lookup(
            AccountProviderResearchInput(
                organisation_id=self.organisation_id,
                company_id=uuid4(),
                name=name,
                domain=domain,
                website=website,
                linkedin_url=linkedin_url,
            )
        )
        if provider_result.status != "found":
            if domain or website or name:
                write_result = await self._create_account_without_resolution(
                    AccountCreateCommand(
                        name=name,
                        domain=domain,
                        website=website,
                        linkedin_url=linkedin_url,
                    ),
                    context=command.context,
                )
                return AccountIdentityResolutionResult(
                    status="resolved",
                    account=write_result.account,
                    created=write_result.created,
                    matched_by=_identity_matched_by(write_result.matched_by),
                )
            return AccountIdentityResolutionResult(
                status=provider_result.status,
                error_detail=_public_provider_error(
                    provider_result.status,
                    provider_result.error_detail,
                    fallback="LinkedIn research could not resolve this account",
                ),
            )

        write_result = await self._write_provider_account(
            provider_result,
            command.context,
            requested_linkedin_url=linkedin_url,
        )
        await AccountResearchService(self.session, self.organisation_id).persist_provider_result(
            write_result.account.company_id,
            provider_result,
            commit=False,
        )
        return AccountIdentityResolutionResult(
            status="resolved",
            account=write_result.account,
            created=write_result.created,
            matched_by=(
                "apollo" if write_result.created else _identity_matched_by(write_result.matched_by)
            ),
        )

    async def _write_provider_account(
        self,
        provider_result: AccountProviderResearchResult,
        context: AccountWriteContext | None,
        *,
        requested_linkedin_url: str | None = None,
    ) -> AccountWriteResult:
        linkedin_url = clean_optional_string(provider_result.linkedin_url)
        if linkedin_url and linkedin_url.lower() != (requested_linkedin_url or "").lower():
            account = await self._find_account_by_linkedin_url(linkedin_url)
            if account:
                return AccountWriteResult(account=account, created=False, matched_by="linkedin")

        domain = normalize_account_domain(provider_result.domain or provider_result.website)
        if domain and hasattr(self.account_writer, "find_accounts_by_domain"):
            matches = await self.account_writer.find_accounts_by_domain(domain)
            if len(matches) == 1:
                return AccountWriteResult(account=matches[0], created=False, matched_by="domain")

        return await self._create_account_without_resolution(
            AccountCreateCommand(
                name=provider_result.name or domain,
                domain=domain,
                website=provider_result.website,
                industry=provider_result.industry,
                linkedin_url=provider_result.linkedin_url,
            ),
            context=context,
        )

    async def _create_account_without_resolution(
        self,
        command: AccountCreateCommand,
        *,
        context: AccountWriteContext | None,
    ) -> AccountWriteResult:
        raw_create = getattr(self.account_writer, "_create_account_record", None)
        if raw_create is not None:
            return await raw_create(command, context=context)
        return await self.account_writer.create_account(command, context=context)

    async def _find_account_by_linkedin_url(self, linkedin_url: str) -> CRMCompany | None:
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.is_deleted).is_(False),
            func.lower(func.trim(col(CRMCompany.linkedin_url))) == linkedin_url.lower(),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _find_account_by_domain(self, domain: str) -> CRMCompany | None:
        stmt = select(CRMCompany).where(
            col(CRMCompany.organisation_id) == self.organisation_id,
            col(CRMCompany.is_deleted).is_(False),
            func.lower(func.trim(col(CRMCompany.domain))) == domain.lower(),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()


def _public_provider_error(
    status: AccountIdentityResolutionStatus,
    detail: str | None,
    *,
    fallback: str,
) -> str:
    if status == "error" or (detail and "apollo" in detail.lower()):
        return fallback
    return detail or fallback


def _identity_matched_by(value: str) -> AccountIdentityMatchedBy | None:
    if value in {"linkedin", "domain", "apollo", "created"}:
        return cast(AccountIdentityMatchedBy, value)
    return None
