"""Provider-first account research workflow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from src.agents.workflows.account_research_summary import AccountResearchSummaryAgent
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.accounts.domain_resolution_service import AccountDomainResolutionService
from src.service.crm.accounts.research_service import (
    AccountProviderLookup,
    AccountProviderResearchInput,
    AccountResearchService,
    AccountResearchSummaryResult,
    research_account_with_apollo,
)
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService

ProviderLookup = Callable[[AccountProviderResearchInput], Awaitable]
SummaryGenerator = Callable[[dict], Awaitable[AccountResearchSummaryResult]]

logger = get_logger(__name__)


class AccountResearchWorkflowError(Exception):
    """Retry-aware error raised by account research orchestration."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class AccountResearchWorkflowResult:
    """Small task result for account provider enrichment."""

    provider_status: str
    summary_status: str
    summary_markdown: str | None = None


@dataclass(frozen=True)
class _AccountResearchSnapshot:
    """Session-free account data needed for one provider lookup."""

    company_id: UUID
    name: str | None
    domain: str | None
    website: str | None
    linkedin_url: str | None
    company_enrichment: dict[str, Any]


class AccountResearchWorkflowService:
    """Run provider-first research for one CRM account."""

    def __init__(
        self,
        organisation_id: UUID,
        *,
        provider_lookup: AccountProviderLookup = research_account_with_apollo,
        summary_generator: SummaryGenerator | None = None,
    ):
        self.organisation_id = organisation_id
        self.provider_lookup = provider_lookup
        self.summary_generator = summary_generator

    async def research_account(
        self,
        company_id: UUID,
        *,
        job_id: UUID | None = None,
    ) -> AccountResearchWorkflowResult:
        """Research one account and return provider status."""
        snapshot = await self._load_snapshot(company_id)
        if snapshot is None:
            raise AccountResearchWorkflowError("Account not found", retryable=False)

        if snapshot.company_enrichment:
            provider_status = "cached"
            company_enrichment = snapshot.company_enrichment
        elif not snapshot.domain and not snapshot.linkedin_url:
            return AccountResearchWorkflowResult(
                provider_status="skipped",
                summary_status="skipped",
            )
        else:
            provider_result = await self.provider_lookup(
                AccountProviderResearchInput(
                    organisation_id=self.organisation_id,
                    company_id=snapshot.company_id,
                    name=snapshot.name,
                    domain=snapshot.domain,
                    website=snapshot.website,
                    linkedin_url=snapshot.linkedin_url,
                )
            )

            if provider_result.status == "error":
                raise AccountResearchWorkflowError(
                    provider_result.error_detail or "Account provider research failed",
                    retryable=True,
                )

            async for session in acquire_org_async_session(self.organisation_id):
                await AccountResearchService(session, self.organisation_id).persist_provider_result(
                    snapshot.company_id,
                    provider_result,
                )
                break

            provider_status = provider_result.status
            company_enrichment = provider_result.company_enrichment or {}

        if provider_status not in {"found", "cached"} or not company_enrichment:
            await self._record_completion_activity(
                snapshot.company_id,
                job_id=job_id,
                provider_status=provider_status,
            )
            return AccountResearchWorkflowResult(
                provider_status=provider_status,
                summary_status="skipped",
            )

        summary_result = await self._generate_summary(
            snapshot.company_id,
            company_enrichment,
        )
        if summary_result.status == "generated":
            async for session in acquire_org_async_session(self.organisation_id):
                await AccountResearchService(session, self.organisation_id).persist_summary(
                    snapshot.company_id,
                    summary_result,
                )
                break

        await self._record_completion_activity(
            snapshot.company_id,
            job_id=job_id,
            provider_status=provider_status,
        )
        return AccountResearchWorkflowResult(
            provider_status=provider_status,
            summary_status=summary_result.status,
            summary_markdown=summary_result.summary_markdown,
        )

    async def _generate_summary(
        self,
        company_id: UUID,
        company_enrichment: dict,
    ) -> AccountResearchSummaryResult:
        if self.summary_generator:
            return await self.summary_generator(company_enrichment)
        agent = AccountResearchSummaryAgent(self.organisation_id)
        try:
            return await agent.summarize(
                company_id=company_id,
                company_enrichment=company_enrichment,
            )
        except Exception as exc:
            logger.warning(
                "Account summary generation failed",
                extra={
                    "event": "account_research_summary_failed",
                    "organisation_id": str(self.organisation_id),
                    "company_id": str(company_id),
                    "failure_reason": str(exc),
                },
                exc_info=True,
            )
            return AccountResearchSummaryResult(status="error", error_detail=str(exc))

    async def _load_snapshot(self, company_id: UUID) -> _AccountResearchSnapshot | None:
        """Load account context, then let the DB session close before provider calls."""
        async for session in acquire_org_async_session(self.organisation_id):
            account = await session.get(CRMCompany, company_id)
            if not account or account.organisation_id != self.organisation_id or account.is_deleted:
                return None

            domain = self._safe_domain(account.domain or account.website)
            linkedin_url = (account.linkedin_url or "").strip() or None
            return _AccountResearchSnapshot(
                company_id=account.company_id,
                name=account.name,
                domain=domain,
                website=account.website,
                linkedin_url=linkedin_url,
                company_enrichment=dict(account.company_enrichment or {}),
            )
        return None

    async def _record_completion_activity(
        self,
        company_id: UUID,
        *,
        job_id: UUID | None,
        provider_status: str,
    ) -> None:
        if job_id is None:
            return

        async for session in acquire_org_async_session(self.organisation_id):
            account = await session.get(CRMCompany, company_id)
            if not account or account.organisation_id != self.organisation_id or account.is_deleted:
                return
            await ActivityFeedProjectionService(
                session,
                self.organisation_id,
            ).upsert_account_enrichment_completed(
                account,
                job_id=job_id,
                provider_status=provider_status,
            )
            await session.commit()
            break

    @staticmethod
    def _safe_domain(value: str | None) -> str | None:
        domain = AccountDomainResolutionService.normalize_domain(value)
        if not AccountDomainResolutionService.domain_is_safe_for_account_resolution(domain):
            return None
        return domain
