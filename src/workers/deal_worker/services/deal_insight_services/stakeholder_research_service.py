"""Stakeholder research service — orchestrates StakeholderResearchAgent.

Three-phase fresh session pattern:
- Phase 1: Load stakeholder record (fresh session, release before LLM)
- Phase 2: Run StakeholderResearchAgent (agent manages its own sessions via tools)
- Phase 3: Save research markdown (fresh session)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlmodel import col
from src.agents.common.token_tracking import TokenUsageData
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
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


class StakeholderResearchService:
    """Service for researching individual stakeholders via Apollo/LinkedIn.

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

        Args:
            stakeholder_id: Required — unique within the org schema
            deal_id: Optional — passed to agent for tool context
            force_refresh: Skip cooldown check

        Returns:
            The markdown research profile

        Raises:
            StakeholderResearchError: If stakeholder not found or agent fails
        """
        from src.agents.workflows.stakeholder_research import StakeholderResearchAgent

        # Phase 1: Load stakeholder context (fresh session)
        async for session in acquire_org_async_session(self.organisation_id):
            stakeholder = await session.get(DealStakeholder, stakeholder_id)
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

            if stakeholder.approval_status != ApprovalStatus.APPROVED:
                raise StakeholderResearchError(
                    f"Stakeholder not approved (status={stakeholder.approval_status})",
                    retryable=False,
                )

            s_email = stakeholder.email
            s_name = stakeholder.name
            s_deal_id = deal_id or stakeholder.deal_id

            # Email-based cooldown: check if ANY stakeholder with this email
            # was recently researched (same person can appear on multiple deals)
            if not force_refresh:
                recent_stmt = (
                    select(DealStakeholder)
                    .where(
                        col(DealStakeholder.organisation_id) == self.organisation_id,
                        col(DealStakeholder.email) == s_email,
                        col(DealStakeholder.last_researched_at).isnot(None),
                    )
                    .order_by(col(DealStakeholder.last_researched_at).desc())
                    .limit(1)
                )
                result = await session.execute(recent_stmt)
                already_researched = result.scalar_one_or_none()

                if already_researched and already_researched.last_researched_at:
                    age = datetime.now(UTC) - already_researched.last_researched_at
                    if age < timedelta(days=RESEARCH_COOLDOWN_DAYS):
                        # Copy research to this record and return
                        if already_researched.stakeholder_research:
                            copy_stmt = (
                                update(DealStakeholder)
                                .where(
                                    DealStakeholder.__table__.c.organisation_id
                                    == self.organisation_id,
                                    DealStakeholder.__table__.c.stakeholder_id == stakeholder_id,
                                )
                                .values(
                                    stakeholder_research=already_researched.stakeholder_research,
                                    last_researched_at=already_researched.last_researched_at,
                                )
                            )
                            await session.execute(copy_stmt)
                            await session.commit()
                        logger.info(
                            "Stakeholder %s: copied research from email=%s, skipping agent",
                            stakeholder_id,
                            s_email,
                        )
                        return already_researched.stakeholder_research or ""
        # Session released

        # Get org name from master schema
        org_name: str | None = None
        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, self.organisation_id)

        # Phase 2: Run agent (no session held)
        agent = StakeholderResearchAgent(
            organisation_id=self.organisation_id,
            deal_id=s_deal_id,
            org_name=org_name,
        )

        try:
            markdown = await agent.research(
                stakeholder_id=stakeholder_id,
                email=s_email,
                name=s_name,
            )
            self.last_token_usage = agent.last_token_usage
        except Exception as exc:
            raise StakeholderResearchError(
                f"Agent failed: {exc}",
                retryable=True,
            ) from exc

        # Phase 3: Save research markdown (fresh session)
        now = datetime.now(UTC)
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = (
                update(DealStakeholder)
                .where(
                    DealStakeholder.__table__.c.organisation_id == self.organisation_id,
                    DealStakeholder.__table__.c.stakeholder_id == stakeholder_id,
                )
                .values(
                    stakeholder_research=markdown,
                    last_researched_at=now,
                    updated_at=now,
                )
            )
            await session.execute(stmt)
            await session.commit()

        logger.info("Stakeholder %s research completed", stakeholder_id)
        return markdown
