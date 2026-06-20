"""Stakeholder research service — orchestrates StakeholderResearchAgent.

Three-phase fresh session pattern:
- Phase 1: Load stakeholder context (fresh session, release before LLM)
- Phase 2: Run StakeholderResearchAgent (returns structured StakeholderResearchResult)
- Phase 3: Persist identity fields + research markdown (fresh session)

The service is the single DB writer — the tool is read-only during research.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from src.agents.common.token_tracking import TokenUsageData
from src.agents.tools.crm.enrich_stakeholder import _infer_department
from src.agents.workflows.stakeholder_research.models import StakeholderResearchResult
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.utils.deal_job_utils import get_organisation_name

logger = get_logger(__name__)

RESEARCH_COOLDOWN_DAYS = 30


class StakeholderResearchError(Exception):
    """Error during stakeholder research."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass
class _StakeholderContext:
    """Snapshot of a stakeholder loaded for research, with session released."""

    email: str
    name: str | None
    deal_id: UUID
    contact_id: UUID | None = None
    external_links: list[dict[str, Any]] = field(default_factory=list)


class StakeholderResearchService:
    """Service for researching individual stakeholders via Apollo.

    Usage:
        service = StakeholderResearchService(org_id)
        markdown = await service.research_stakeholder(stakeholder_id, deal_id=deal_id)
        token_usage = service.last_token_usage
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self.last_token_usage: TokenUsageData | None = None

    async def research_stakeholder(
        self,
        stakeholder_id: UUID,
        deal_id: UUID | None = None,
        force_refresh: bool = False,
    ) -> str:
        """Research a single stakeholder and persist results.

        Returns the markdown research profile.

        Raises:
            StakeholderResearchError: If stakeholder not found or agent fails.
        """
        # Phase 1: Load context, check cooldown, release session
        ctx = await self._load_context(stakeholder_id, deal_id)

        if not force_refresh:
            cached = await self._try_copy_cached_research(stakeholder_id, ctx)
            if cached is not None:
                return cached

        # Phase 2: Run agent (no DB session held)
        result = await self._run_agent(stakeholder_id, ctx)

        # Phase 3: Persist result based on status
        return await self._persist_result(stakeholder_id, ctx, result)

    async def _load_context(
        self,
        stakeholder_id: UUID,
        override_deal_id: UUID | None,
    ) -> _StakeholderContext:
        """Load and validate the stakeholder, returning a session-free snapshot."""
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(CRMDealParticipant).where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.participant_id) == stakeholder_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
                col(CRMDealParticipant.is_deleted).is_(False),
            )
            if override_deal_id:
                stmt = stmt.where(col(CRMDealParticipant.deal_id) == override_deal_id)
            result = await session.execute(stmt)
            stakeholder = result.scalar_one_or_none()

            if not stakeholder:
                raise StakeholderResearchError(
                    f"Stakeholder {stakeholder_id} not found",
                    retryable=False,
                )

            if stakeholder.organisation_id != self.organisation_id:
                raise StakeholderResearchError(
                    "Organisation mismatch",
                    retryable=False,
                )

            if _status_value(stakeholder.approval_status) != ApprovalStatus.APPROVED.value:
                raise StakeholderResearchError(
                    f"Stakeholder not approved (status={stakeholder.approval_status})",
                    retryable=False,
                )

            contact = await self._load_contact(session, stakeholder.related_local_id)
            email = _normalize_email(
                contact.email if contact and contact.email else stakeholder.related_email
            )
            if not email:
                raise StakeholderResearchError(
                    f"Stakeholder {stakeholder_id} has no email",
                    retryable=False,
                )

            contact_properties = contact.properties if contact else {}
            contact_links = (
                contact_properties.get("external_links")
                if isinstance(contact_properties, dict)
                else None
            )
            external_links = (
                contact_links
                if isinstance(contact_links, list)
                else list(stakeholder.external_links or [])
            )
            contact_name = contact.full_name if contact else None

            return _StakeholderContext(
                email=email,
                name=contact_name or stakeholder.related_name,
                deal_id=override_deal_id or stakeholder.deal_id,
                contact_id=contact.contact_id if contact else stakeholder.related_local_id,
                external_links=list(external_links),
            )

        # acquire_org_async_session always yields exactly one session, so this
        # branch is unreachable. Raising keeps the type checker happy.
        raise StakeholderResearchError("Failed to acquire org session", retryable=True)

    async def _try_copy_cached_research(
        self,
        stakeholder_id: UUID,
        ctx: _StakeholderContext,
    ) -> str | None:
        """Copy research from another stakeholder with the same email if recent.

        Returns the copied markdown when a cache hit occurs, None otherwise.
        Same person can appear on multiple deals — research is per-person, not per-deal.
        """
        cutoff = datetime.now(UTC) - timedelta(days=RESEARCH_COOLDOWN_DAYS)

        async for session in acquire_org_async_session(self.organisation_id):
            recent = await self._find_recent_research(session, ctx.email, cutoff)
            if not recent or not recent.contact_research:
                return None

            await session.execute(
                update(CRMDealParticipant)
                .where(
                    col(CRMDealParticipant.organisation_id) == self.organisation_id,
                    col(CRMDealParticipant.participant_id) == stakeholder_id,
                )
                .values(
                    stakeholder_research=recent.contact_research,
                    last_researched_at=recent.last_researched_at,
                    updated_at=datetime.now(UTC),
                )
            )
            if ctx.contact_id and recent.contact_id != ctx.contact_id:
                await session.execute(
                    update(CRMContact)
                    .where(
                        col(CRMContact.organisation_id) == self.organisation_id,
                        col(CRMContact.contact_id) == ctx.contact_id,
                    )
                    .values(
                        contact_research=recent.contact_research,
                        last_researched_at=recent.last_researched_at,
                        updated_at=datetime.now(UTC),
                    )
                )
            await session.commit()

            logger.info(
                "Stakeholder %s: copied research from email=%s, skipping agent",
                stakeholder_id,
                ctx.email,
                extra={
                    "event": "stakeholder_research_cache_hit",
                    "organisation_id": str(self.organisation_id),
                    "stakeholder_id": str(stakeholder_id),
                    "email": ctx.email,
                },
            )
            return recent.contact_research

        return None

    async def _find_recent_research(
        self,
        session: AsyncSession,
        email: str,
        cutoff: datetime,
    ) -> CRMContact | None:
        """Find the most recently researched stakeholder with this email."""
        stmt = (
            select(CRMContact)
            .where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(func.trim(col(CRMContact.email))) == _normalize_email(email),
                col(CRMContact.contact_research).isnot(None),
                col(CRMContact.last_researched_at) >= cutoff,
            )
            .order_by(col(CRMContact.last_researched_at).desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _run_agent(
        self,
        stakeholder_id: UUID,
        ctx: _StakeholderContext,
    ) -> StakeholderResearchResult:
        """Run the research agent. Token usage is captured on the service."""
        from src.agents.workflows.stakeholder_research import StakeholderResearchAgent

        org_name: str | None = None
        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, self.organisation_id)

        agent = StakeholderResearchAgent(
            organisation_id=self.organisation_id,
            deal_id=ctx.deal_id,
            org_name=org_name,
        )

        try:
            result = await agent.research(
                stakeholder_id=stakeholder_id,
                email=ctx.email,
                name=ctx.name,
            )
        except Exception as exc:
            raise StakeholderResearchError(
                f"Agent failed: {exc}",
                retryable=True,
            ) from exc

        self.last_token_usage = agent.last_token_usage
        return result

    async def _persist_result(
        self,
        stakeholder_id: UUID,
        ctx: _StakeholderContext,
        result: StakeholderResearchResult,
    ) -> str:
        """Apply the structured result to the DB based on status."""
        if result.status == "error":
            # Tool-level failure — leave last_researched_at NULL so the periodic
            # retry picks it up. Don't write anything.
            logger.warning(
                "Stakeholder %s enrichment error, will retry later: %s",
                stakeholder_id,
                result.error_detail,
                extra={
                    "event": "stakeholder_research_agent_error",
                    "organisation_id": str(self.organisation_id),
                    "stakeholder_id": str(stakeholder_id),
                    "error": result.error_detail,
                    "deal_id": str(ctx.deal_id),
                },
            )
            raise StakeholderResearchError(
                f"Enrichment failed: {result.error_detail}",
                retryable=True,
            )

        now = datetime.now(UTC)
        participant_values: dict[str, Any] = {"updated_at": now, "last_researched_at": now}
        contact_values: dict[str, Any] = {"updated_at": now, "last_researched_at": now}
        contact_properties: dict[str, Any] = {}

        if result.status == "not_found":
            # Person genuinely not found — record we tried so we don't retry forever,
            # but store NULL instead of junk text.
            participant_values["stakeholder_research"] = None
            contact_values["contact_research"] = None
            await self._write_research_values(
                stakeholder_id,
                ctx.contact_id,
                participant_values,
                contact_values,
                contact_properties,
            )
            logger.info(
                "Stakeholder %s: no public profile found",
                stakeholder_id,
                extra={
                    "event": "stakeholder_research_not_found",
                    "organisation_id": str(self.organisation_id),
                    "stakeholder_id": str(stakeholder_id),
                    "deal_id": str(ctx.deal_id),
                    "contact_id": str(ctx.contact_id) if ctx.contact_id else None,
                },
            )
            return ""

        # status == "found" — persist identity fields + the LLM-written markdown
        markdown = result.research_markdown or ""
        participant_values["stakeholder_research"] = markdown
        contact_values["contact_research"] = markdown

        if self._should_overwrite_name(ctx.name, result.name):
            participant_values["related_name"] = result.name
            contact_values["full_name"] = result.name

        if result.job_title:
            participant_values["job_title"] = result.job_title
            contact_values["job_title"] = result.job_title

        if result.first_name:
            contact_values["first_name"] = result.first_name
        if result.last_name:
            contact_values["last_name"] = result.last_name

        department = _infer_department(result.job_title)
        if department:
            participant_values["department"] = department
            contact_properties["department"] = department

        if result.linkedin_url:
            participant_values["linkedin_url"] = result.linkedin_url
            contact_values["linkedin_url"] = result.linkedin_url
            if not self._has_linkedin(ctx.external_links):
                participant_values["external_links"] = ctx.external_links + [
                    {
                        "platform": "linkedin",
                        "url": result.linkedin_url,
                        "label": "LinkedIn",
                        "added_at": now.isoformat(),
                    }
                ]
                contact_properties["external_links"] = participant_values["external_links"]

        await self._write_research_values(
            stakeholder_id,
            ctx.contact_id,
            participant_values,
            contact_values,
            contact_properties,
        )

        logger.info(
            "Stakeholder %s research completed",
            stakeholder_id,
            extra={
                "event": "stakeholder_research_completed",
                "organisation_id": str(self.organisation_id),
                "stakeholder_id": str(stakeholder_id),
                "deal_id": str(ctx.deal_id),
                "contact_id": str(ctx.contact_id) if ctx.contact_id else None,
            },
        )
        return markdown

    async def _write_values(
        self,
        stakeholder_id: UUID,
        values: dict[str, Any],
    ) -> None:
        """Apply an update to a participant-backed stakeholder row."""
        async for session in acquire_org_async_session(self.organisation_id):
            await session.execute(
                update(CRMDealParticipant)
                .where(
                    col(CRMDealParticipant.organisation_id) == self.organisation_id,
                    col(CRMDealParticipant.participant_id) == stakeholder_id,
                )
                .values(**values)
            )
            await session.commit()
            return

    async def _write_research_values(
        self,
        stakeholder_id: UUID,
        contact_id: UUID | None,
        participant_values: dict[str, Any],
        contact_values: dict[str, Any],
        contact_properties: dict[str, Any],
    ) -> None:
        async for session in acquire_org_async_session(self.organisation_id):
            await session.execute(
                update(CRMDealParticipant)
                .where(
                    col(CRMDealParticipant.organisation_id) == self.organisation_id,
                    col(CRMDealParticipant.participant_id) == stakeholder_id,
                )
                .values(**participant_values)
            )

            if contact_id:
                contact = await session.get(CRMContact, contact_id)
                if contact and contact.organisation_id == self.organisation_id:
                    values = dict(contact_values)
                    if contact_properties:
                        properties = dict(contact.properties or {})
                        properties.update(contact_properties)
                        values["properties"] = properties
                    await session.execute(
                        update(CRMContact)
                        .where(
                            col(CRMContact.organisation_id) == self.organisation_id,
                            col(CRMContact.contact_id) == contact_id,
                        )
                        .values(**values)
                    )

            await session.commit()
            return

    async def _fill_contact_identity(
        self,
        stakeholder_id: UUID,
        contact_id: UUID,
        result: StakeholderResearchResult,
    ) -> None:
        """Fill missing identity fields on the linked CRM contact.

        Only writes fields that are currently empty. CRM-sourced fields are
        left alone — the CRM is the source of truth and any overwrite would
        be reverted on the next sync.
        """
        async for session in acquire_org_async_session(self.organisation_id):
            contact = await session.get(CRMContact, contact_id)
            if not contact or contact.organisation_id != self.organisation_id:
                return

            values: dict[str, Any] = {}

            if not contact.first_name and result.first_name:
                values["first_name"] = result.first_name
            if not contact.last_name and result.last_name:
                values["last_name"] = result.last_name
            if not contact.full_name and result.name:
                values["full_name"] = result.name
            if not contact.job_title and result.job_title:
                values["job_title"] = result.job_title

            if not values:
                return

            await session.execute(
                update(CRMContact)
                .where(
                    col(CRMContact.organisation_id) == self.organisation_id,
                    col(CRMContact.contact_id) == contact_id,
                )
                .values(**values)
            )
            await session.commit()
            logger.info(
                "Stakeholder research filled %d empty field(s) on contact %s: %s",
                len(values),
                contact_id,
                ", ".join(values.keys()),
                extra={
                    "event": "stakeholder_research_contact_update",
                    "organisation_id": str(self.organisation_id),
                    "contact_id": str(contact_id),
                    "stakeholder_id": str(stakeholder_id),
                    "filled_fields": list(values.keys()),
                },
            )
            return

    @staticmethod
    def _should_overwrite_name(existing: str | None, new: str | None) -> bool:
        """Only overwrite the name if Apollo gave us something more complete."""
        if not new:
            return False
        if not existing:
            return True
        return len(new) > len(existing)

    @staticmethod
    def _has_linkedin(links: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(link, dict) and link.get("platform", "").lower() == "linkedin"
            for link in links
        )

    async def _load_contact(
        self,
        session: AsyncSession,
        contact_id: UUID | None,
    ) -> CRMContact | None:
        if not contact_id:
            return None
        contact = await session.get(CRMContact, contact_id)
        if not contact or contact.organisation_id != self.organisation_id or contact.is_deleted:
            return None
        return contact


def _normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized or None


def _status_value(status: object) -> str:
    return str(getattr(status, "value", status))
