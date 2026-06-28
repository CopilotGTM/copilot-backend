"""Provider-first contact research workflow.

Called by contact research Celery tasks. The workflow loads a tenant-owned
contact snapshot, releases the org session before provider/LLM calls, persists
structured provider fields first, then optionally writes a short no-tool summary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from src.agents.workflows.contact_research_summary import ContactResearchSummaryAgent
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.accounts.domain_resolution_service import AccountDomainResolutionService
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.contacts.research_service import (
    ContactProviderLookup,
    ContactProviderResearchInput,
    ContactResearchService,
    ContactResearchSummaryResult,
    research_contact_with_apollo,
)

logger = get_logger(__name__)

SummaryGenerator = Callable[[dict], Awaitable[ContactResearchSummaryResult]]


class ContactResearchWorkflowError(Exception):
    """Retry-aware error raised by contact research orchestration."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ContactResearchWorkflowResult:
    """Small task result that keeps provider and summary outcomes separate."""

    provider_status: str
    summary_status: str
    summary_markdown: str | None = None


@dataclass(frozen=True)
class _ContactResearchSnapshot:
    """Session-free contact data needed for one provider lookup."""

    contact_id: UUID
    email: str
    name: str | None
    company_name: str | None
    company_domain: str | None
    linkedin_url: str | None


class ContactResearchWorkflowService:
    """Run provider-first research for one CRM contact."""

    def __init__(
        self,
        organisation_id: UUID,
        *,
        provider_lookup: ContactProviderLookup = research_contact_with_apollo,
        summary_generator: SummaryGenerator | None = None,
    ):
        self.organisation_id = organisation_id
        self.provider_lookup = provider_lookup
        self.summary_generator = summary_generator

    async def research_contact(
        self,
        contact_id: UUID,
        *,
        job_id: UUID | None = None,
    ) -> ContactResearchWorkflowResult:
        """Research one contact and return independent provider/summary statuses."""
        snapshot = await self._load_snapshot(contact_id)
        if snapshot is None:
            raise ContactResearchWorkflowError("Contact not found", retryable=False)

        if not snapshot.email or not snapshot.company_domain:
            return ContactResearchWorkflowResult(
                provider_status="skipped",
                summary_status="skipped",
            )

        provider_result = await self.provider_lookup(
            ContactProviderResearchInput(
                organisation_id=self.organisation_id,
                contact_id=snapshot.contact_id,
                email=snapshot.email,
                name=snapshot.name,
                company_name=snapshot.company_name,
                company_domain=snapshot.company_domain,
                linkedin_url=snapshot.linkedin_url,
            )
        )

        if provider_result.status == "error":
            raise ContactResearchWorkflowError(
                provider_result.error_detail or "Contact provider research failed",
                retryable=True,
            )

        # Persist provider result before summary generation. A model failure
        # should not discard durable fields from Apollo/provider enrichment.
        async for session in acquire_org_async_session(self.organisation_id):
            await ContactResearchService(session, self.organisation_id).persist_provider_result(
                snapshot.contact_id,
                provider_result,
            )
            break

        if provider_result.status != "found" or not provider_result.profile_enrichment:
            await self._record_completion_activity(
                snapshot.contact_id,
                job_id=job_id,
                provider_status=provider_result.status,
                summary_status="skipped",
            )
            return ContactResearchWorkflowResult(
                provider_status=provider_result.status,
                summary_status="skipped",
            )

        summary_result = await self._generate_summary(
            snapshot.contact_id,
            provider_result.profile_enrichment,
        )
        if summary_result.status == "generated":
            async for session in acquire_org_async_session(self.organisation_id):
                await ContactResearchService(session, self.organisation_id).persist_summary(
                    snapshot.contact_id,
                    summary_result,
                )
                break

        await self._record_completion_activity(
            snapshot.contact_id,
            job_id=job_id,
            provider_status=provider_result.status,
            summary_status=summary_result.status,
        )
        return ContactResearchWorkflowResult(
            provider_status=provider_result.status,
            summary_status=summary_result.status,
            summary_markdown=summary_result.summary_markdown,
        )

    async def _load_snapshot(self, contact_id: UUID) -> _ContactResearchSnapshot | None:
        """Load contact context, then let the DB session close before provider calls."""
        async for session in acquire_org_async_session(self.organisation_id):
            contact = await session.get(CRMContact, contact_id)
            if not contact or contact.organisation_id != self.organisation_id or contact.is_deleted:
                return None

            email = (contact.email or "").strip().lower()
            domain = AccountDomainResolutionService.derive_safe_domain_from_email(email)
            return _ContactResearchSnapshot(
                contact_id=contact.contact_id,
                email=email,
                name=contact.full_name,
                company_name=contact.company_name,
                company_domain=domain,
                linkedin_url=contact.linkedin_url,
            )
        return None

    async def _generate_summary(
        self,
        contact_id: UUID,
        profile_enrichment: dict,
    ) -> ContactResearchSummaryResult:
        if self.summary_generator:
            return await self.summary_generator(profile_enrichment)
        agent = ContactResearchSummaryAgent(self.organisation_id)
        try:
            return await agent.summarize(
                contact_id=contact_id,
                profile_enrichment=profile_enrichment,
            )
        except Exception as exc:
            logger.warning(
                "Contact summary generation failed",
                extra={
                    "event": "contact_research_summary_failed",
                    "organisation_id": str(self.organisation_id),
                    "contact_id": str(contact_id),
                    "failure_reason": str(exc),
                },
                exc_info=True,
            )
            return ContactResearchSummaryResult(status="error", error_detail=str(exc))

    async def _record_completion_activity(
        self,
        contact_id: UUID,
        *,
        job_id: UUID | None,
        provider_status: str,
        summary_status: str,
    ) -> None:
        """Write the visible enrichment completion card after terminal statuses are known."""
        if job_id is None:
            return

        async for session in acquire_org_async_session(self.organisation_id):
            contact = await session.get(CRMContact, contact_id)
            if not contact or contact.organisation_id != self.organisation_id or contact.is_deleted:
                return
            await ActivityFeedProjectionService(
                session,
                self.organisation_id,
            ).upsert_contact_enrichment_completed(
                contact,
                job_id=job_id,
                provider_status=provider_status,
                summary_status=summary_status,
            )
            await session.commit()
            break
