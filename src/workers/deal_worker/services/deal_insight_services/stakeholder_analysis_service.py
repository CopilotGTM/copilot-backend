"""Stakeholder analysis service for processing stakeholder enrichment.

This module provides a service for analyzing stakeholders using an AI agent
that gathers meetings, emails, and other deal data via tools.

The AI workflow only enriches per-stakeholder fields. Any user-facing
stakeholder alerting is handled by deterministic, stage-aware rules in the
service layer so we do not generate broad stakeholder "insight" noise on
early-stage deals.

Follows fresh session pattern:
- Phase 1: Validate deal, get org_name, check stakeholders exist (fresh session, release before LLM)
- Phase 2: Call agent (agent gathers all data via tools)
- Phase 3: Save result (fresh session)
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlmodel import col
from src.agents.workflows.stakeholder_analysis import (
    DealStakeholdersAnalysisData,
    StakeholderAnalysisAgent,
    StakeholderAnalysisData,
)
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
    DecisionPower,
)
from src.domain.models.organisation_tables.sync.stakeholder_insight_models import (
    DealStakeholderInsight,
    InsightActionType,
    StakeholderInsightPriority,
    StakeholderInsightStatus,
    StakeholderInsightType,
    create_insight_action,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.utils.deal_job_utils import (
    get_deal,
    get_organisation_name,
)

logger = get_logger(__name__)

# Constants
MAX_ENGAGEMENT_HISTORY_ENTRIES = 15  # Keep last 15 snapshots
LATE_STAGE_DECISION_MAKER_TOKENS = {
    "proposal",
    "negotiation",
    "commercial",
    "pricing",
    "quote",
    "contract",
    "legal",
    "procurement",
    "approval",
    "closing",
    "final",
}


class StakeholderAnalysisService:
    """Service for analyzing stakeholders using AI agent with tools.

    This service:
    1. Validates deal exists and checks stakeholders are present
    2. Creates StakeholderAnalysisAgent with org_id + deal_id (agent gathers all data via tools)
    3. Updates stakeholder records with analysis results and saves insights

    Follows fresh session pattern:
    - Phase 1: Validate deal, get org_name, check stakeholders (fresh session, release before LLM)
    - Phase 2: Call agent (agent gathers all data via tools)
    - Phase 3: Save result (fresh session)
    """

    def __init__(self, organisation_id: UUID):
        """Initialize the service.

        SECURITY BOUNDARY: organisation_id is immutable - all queries use this value.

        Args:
            organisation_id: UUID of the organisation (IMMUTABLE - security boundary)
        """
        self.organisation_id = organisation_id
        self.last_token_usage = None

    @staticmethod
    def _normalize_stage_tokens(*values: str | None) -> set[str]:
        """Normalize stage labels/keys into comparable lowercase tokens."""
        tokens: set[str] = set()
        for value in values:
            if not value:
                continue
            normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
            if normalized:
                tokens.add(normalized)
                tokens.update(part for part in normalized.split("_") if part)
        return tokens

    def _deal_requires_decision_maker_coverage(self, deal: CRMDeal) -> bool:
        """Return True when the deal stage is mature enough to expect a decision maker.

        We intentionally avoid firing this on newly created or discovery-stage deals.
        Later commercial stages should have a named decision maker or economic buyer.
        """
        if deal.deal_status != CRMDealStatus.OPEN:
            return False

        stage_tokens = self._normalize_stage_tokens(
            deal.stage_label,
            deal.stage_name,
            deal.forecast_category,
        )

        if stage_tokens & LATE_STAGE_DECISION_MAKER_TOKENS:
            return True

        return bool(deal.probability and deal.probability >= 50.0)

    @staticmethod
    def _has_active_decision_maker(stakeholders: list[DealStakeholder]) -> bool:
        """Return True when a non-rejected stakeholder is tagged as the decision maker."""
        for stakeholder in stakeholders:
            if stakeholder.approval_status in {ApprovalStatus.REJECTED, ApprovalStatus.REMOVED}:
                continue
            if stakeholder.decision_power == DecisionPower.DECISION_MAKER:
                return True
        return False

    def _build_stage_gated_insights(
        self,
        deal: CRMDeal,
        stakeholders: list[DealStakeholder],
    ) -> list[DealStakeholderInsight]:
        """Build the minimal deterministic stakeholder alerts we still want.

        Today we only surface one rule-based alert:
        - missing decision maker on later-stage open deals
        """
        if not self._deal_requires_decision_maker_coverage(deal):
            return []

        if self._has_active_decision_maker(stakeholders):
            return []

        stage_label = deal.stage_label or deal.stage_name or "the current stage"

        return [
            DealStakeholderInsight(
                organisation_id=self.organisation_id,
                deal_id=deal.deal_id,
                stakeholder_id=None,
                insight_type=StakeholderInsightType.MISSING_DECISION_MAKER,
                priority=StakeholderInsightPriority.HIGH,
                title="Decision maker still not identified",
                description=(
                    f"The deal is in {stage_label}, but no active stakeholder is tagged "
                    "as DECISION_MAKER. Before this deal advances further, the team should "
                    "confirm who has final purchase authority."
                ),
                ai_reasoning=(
                    "Deterministic stage-aware rule: later-stage open deals should have "
                    "a named decision maker. This check is intentionally suppressed for "
                    "early-stage deals to avoid noisy alerts."
                ),
                status=StakeholderInsightStatus.ACTIVE,
                ai_confidence=1.0,
                actions=[
                    create_insight_action(InsightActionType.VIEW_DETAILS, label="View Details")
                ],
                source_type="stage_gated_rule",
                identified_at=utcnow(),
            )
        ]

    async def get_stakeholders_for_deal(self, deal_id: UUID) -> list[DealStakeholder]:
        """Get all stakeholders for a deal.

        Args:
            deal_id: UUID of the deal

        Returns:
            List of DealStakeholder objects
        """
        async for session in acquire_org_async_session(self.organisation_id):
            stmt = select(DealStakeholder).where(
                col(DealStakeholder.organisation_id) == self.organisation_id,
                col(DealStakeholder.deal_id) == deal_id,
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
        return []

    async def get_all_deals_with_stakeholders(self) -> list[CRMDeal]:
        """Get all deals that have stakeholders.

        Returns:
            List of CRMDeal objects that have at least one stakeholder
        """
        async for session in acquire_org_async_session(self.organisation_id):
            # Get deal IDs that have stakeholders using a subquery
            stakeholder_subq = (
                select(DealStakeholder)
                .where(col(DealStakeholder.organisation_id) == self.organisation_id)
                .subquery()
            )

            stmt = select(CRMDeal).where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.deal_id).in_(select(stakeholder_subq.c.deal_id).distinct()),
                col(CRMDeal.is_deleted).is_(False),
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
        return []

    def update_stakeholder_with_analysis(
        self, stakeholder: DealStakeholder, analysis: StakeholderAnalysisData
    ) -> None:
        """Update stakeholder record with analysis results.

        Args:
            stakeholder: The stakeholder to update
            analysis: The analysis results
        """
        stakeholder.stance = analysis.stance
        stakeholder.decision_power = analysis.decision_power
        stakeholder.temperature = analysis.temperature
        stakeholder.champion_potential = analysis.champion_potential
        stakeholder.motivation = analysis.motivation if analysis.motivation else None
        stakeholder.pain_points = analysis.pain_points if analysis.pain_points else None
        stakeholder.keywords = analysis.keywords if analysis.keywords else None

        logger.info(
            "Updated stakeholder %s with analysis: stance=%s, decision_power=%s, temperature=%s",
            stakeholder.stakeholder_id,
            analysis.stance,
            analysis.decision_power,
            analysis.temperature,
            extra={
                "event": "stakeholder_analysis_result_saved",
                "organisation_id": str(self.organisation_id),
                "stakeholder_id": str(stakeholder.stakeholder_id),
                "deal_id": str(stakeholder.deal_id),
                "stance": str(analysis.stance),
                "decision_power": str(analysis.decision_power),
                "temperature": str(analysis.temperature),
            },
        )

    async def analyze_all_stakeholders_for_deal(
        self, deal_id: UUID, generate_insights: bool = True
    ) -> dict[UUID, StakeholderAnalysisData | None]:
        """Analyze all stakeholders for a deal using AI agent with tools.

        Phase 1: Validate deal, get org_name, check stakeholders (fresh session)
        Phase 2: Call agent (agent gathers all data via tools)
        Phase 3: Save results (fresh session)

        Args:
            deal_id: UUID of the deal to analyze
            generate_insights: Whether to generate insights (default: True)

        Returns:
            dict mapping each stakeholder UUID to their analysis (None if no result)
        """
        # Phase 1: Validate deal, get org_name, check stakeholders exist
        org_name: str | None = None
        deal: CRMDeal | None = None

        async for session in acquire_org_async_session(self.organisation_id):
            deal = await get_deal(session, self.organisation_id, deal_id)
            if not deal:
                logger.warning(
                    "Deal %s not found in org %s",
                    deal_id,
                    self.organisation_id,
                    extra={
                        "event": "stakeholder_analysis_deal_not_found",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal_id),
                    },
                )
                return {}
        # Session released

        # Org name lives in public schema — use a master session
        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, self.organisation_id)

        stakeholders = await self.get_stakeholders_for_deal(deal_id)
        if not stakeholders:
            logger.info(
                "No stakeholders found for deal %s",
                deal_id,
                extra={
                    "event": "stakeholder_analysis_no_stakeholders",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                },
            )
            return {}

        logger.info(
            "Running stakeholder analysis for %d stakeholders in deal %s",
            len(stakeholders),
            deal_id,
            extra={
                "event": "stakeholder_analysis_started",
                "organisation_id": str(self.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_count": len(stakeholders),
                "generate_insights": generate_insights,
            },
        )

        # Phase 2: Call agent (tools gather all data — meetings, emails, notes, etc.)
        agent = StakeholderAnalysisAgent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            org_name=org_name,
        )
        batch_result: DealStakeholdersAnalysisData = await agent.generate()
        self.last_token_usage = agent.last_token_usage

        # Phase 3: Save result (fresh session)
        email_to_analysis: dict[str, StakeholderAnalysisData] = {
            a.stakeholder_email.lower(): a for a in batch_result.stakeholders
        }

        results: dict[UUID, StakeholderAnalysisData | None] = {}

        async for session in acquire_org_async_session(self.organisation_id):
            # Reload stakeholders in fresh session
            stmt = select(DealStakeholder).where(
                col(DealStakeholder.organisation_id) == self.organisation_id,
                col(DealStakeholder.deal_id) == deal_id,
            )
            result = await session.execute(stmt)
            fresh_stakeholders = list(result.scalars().all())

            for stakeholder in fresh_stakeholders:
                analysis = email_to_analysis.get(stakeholder.email.lower())
                if analysis:
                    # Save current state to history if changed
                    self._save_analysis_snapshot(stakeholder)
                    # Update stakeholder with new analysis
                    self.update_stakeholder_with_analysis(stakeholder, analysis)
                    results[stakeholder.stakeholder_id] = analysis
                else:
                    logger.warning(
                        "No analysis result for stakeholder %s (%s)",
                        stakeholder.stakeholder_id,
                        stakeholder.email,
                        extra={
                            "event": "stakeholder_analysis_missing_result",
                            "organisation_id": str(self.organisation_id),
                            "deal_id": str(deal_id),
                            "stakeholder_id": str(stakeholder.stakeholder_id),
                            "stakeholder_email": stakeholder.email,
                        },
                    )
                    results[stakeholder.stakeholder_id] = None

            if generate_insights and deal is not None:
                await self._expire_old_insights(session, deal_id)
                insights = self._build_stage_gated_insights(deal, fresh_stakeholders)
                for insight in insights:
                    session.add(insight)
                if insights:
                    logger.info(
                        "Added %d stage-gated stakeholder insights for deal %s",
                        len(insights),
                        deal_id,
                        extra={
                            "event": "stakeholder_analysis_stage_gated_insights_added",
                            "organisation_id": str(self.organisation_id),
                            "deal_id": str(deal_id),
                            "insight_count": len(insights),
                        },
                    )

            await session.commit()

        return results

    async def analyze_deal_by_id(self, deal_id: UUID) -> dict[str, int]:
        """Analyze all stakeholders for a deal by its ID.

        Args:
            deal_id: UUID of the deal to analyze

        Returns:
            Dictionary with processing statistics
        """
        results = await self.analyze_all_stakeholders_for_deal(deal_id)

        analyzed = sum(1 for r in results.values() if r is not None)
        skipped = sum(1 for r in results.values() if r is None)

        return {
            "stakeholders_processed": len(results),
            "stakeholders_analyzed": analyzed,
            "stakeholders_skipped": skipped,
        }

    async def analyze_all_stakeholders_for_organisation(self) -> dict[str, int]:
        """Analyze all stakeholders across all deals in the organisation.

        Each deal's stakeholders are analyzed in a single batch LLM call
        for efficiency.

        Returns:
            Dictionary with processing statistics
        """
        stats = {
            "deals_processed": 0,
            "stakeholders_processed": 0,
            "stakeholders_analyzed": 0,
            "stakeholders_skipped": 0,
        }

        deals = await self.get_all_deals_with_stakeholders()
        logger.info(
            "Processing stakeholder analysis for %d deals in org %s",
            len(deals),
            self.organisation_id,
            extra={
                "event": "stakeholder_analysis_batch_started",
                "organisation_id": str(self.organisation_id),
                "deals_to_process": len(deals),
            },
        )

        for deal in deals:
            stats["deals_processed"] += 1
            try:
                results = await self.analyze_all_stakeholders_for_deal(deal.deal_id)
                stats["stakeholders_processed"] += len(results)
                stats["stakeholders_analyzed"] += sum(1 for r in results.values() if r is not None)
                stats["stakeholders_skipped"] += sum(1 for r in results.values() if r is None)
            except Exception as e:
                logger.error(
                    "Error analyzing stakeholders for deal %s: %s",
                    deal.deal_id,
                    e,
                    exc_info=True,
                    extra={
                        "event": "stakeholder_analysis_batch_error",
                        "organisation_id": str(self.organisation_id),
                        "deal_id": str(deal.deal_id),
                        "error": str(e),
                    },
                )
                # Count all stakeholders as skipped on error
                stakeholder_count = len(await self.get_stakeholders_for_deal(deal.deal_id))
                stats["stakeholders_processed"] += stakeholder_count
                stats["stakeholders_skipped"] += stakeholder_count

        return stats

    # ─────────────────────────────────────────────────────────────────────────
    # Insight Generation Methods
    # ─────────────────────────────────────────────────────────────────────────

    def _get_previous_analysis(self, stakeholder: DealStakeholder) -> dict[str, Any] | None:
        """Get most recent historical analysis for comparison.

        Args:
            stakeholder: The stakeholder to get history for

        Returns:
            Most recent history entry or None if no history
        """
        history = stakeholder.stakeholder_engagement_history
        if not history:
            return None

        # History is ordered newest first
        return history[0] if history else None

    def _save_analysis_snapshot(self, stakeholder: DealStakeholder) -> bool:
        """Save current analysis values to history only if there's a change.

        Only saves a new snapshot if key values have changed from the previous
        snapshot. This keeps the history clean and meaningful.

        Args:
            stakeholder: The stakeholder to save history for

        Returns:
            True if snapshot was saved (change detected), False otherwise
        """
        # Create snapshot of current values (handle both enum and string values)
        stance = stakeholder.stance
        stance_val = (stance.value if hasattr(stance, "value") else stance) or "UNKNOWN"
        temp = stakeholder.temperature
        temp_val = (temp.value if hasattr(temp, "value") else temp) or "COLD"
        power = stakeholder.decision_power
        power_val = (power.value if hasattr(power, "value") else power) or "UNKNOWN"

        current_values = {
            "stance": stance_val,
            "temperature": temp_val,
            "engagement_score": stakeholder.engagement_score,
            "champion_potential": stakeholder.champion_potential,
            "decision_power": power_val,
        }

        # Get previous snapshot for comparison
        previous = self._get_previous_analysis(stakeholder)

        # Check if anything changed
        if previous:
            has_change = (
                previous.get("stance") != current_values["stance"]
                or previous.get("temperature") != current_values["temperature"]
                or previous.get("decision_power") != current_values["decision_power"]
                or abs(
                    (previous.get("engagement_score") or 0)
                    - (current_values["engagement_score"] or 0)
                )
                > 0.01
                or abs(
                    (previous.get("champion_potential") or 0)
                    - (current_values["champion_potential"] or 0)
                )
                > 0.01
            )

            if not has_change:
                logger.debug(
                    "No change detected for stakeholder %s, skipping snapshot",
                    stakeholder.stakeholder_id,
                )
                return False

        # Add timestamp for the new snapshot
        snapshot = {
            "recorded_at": utcnow().isoformat(),
            **current_values,
        }

        # Initialize engagement history if needed
        if stakeholder.stakeholder_engagement_history is None:
            stakeholder.stakeholder_engagement_history = []

        # Prepend new snapshot (newest first)
        stakeholder.stakeholder_engagement_history.insert(0, snapshot)

        # Keep only last N entries
        stakeholder.stakeholder_engagement_history = stakeholder.stakeholder_engagement_history[
            :MAX_ENGAGEMENT_HISTORY_ENTRIES
        ]

        logger.info(
            "Change detected for stakeholder %s: stance=%s, engagement=%s - saved snapshot",
            stakeholder.stakeholder_id,
            snapshot["stance"],
            snapshot["engagement_score"],
            extra={
                "event": "stakeholder_analysis_snapshot_saved",
                "organisation_id": str(self.organisation_id),
                "stakeholder_id": str(stakeholder.stakeholder_id),
                "deal_id": str(stakeholder.deal_id),
                "stance": snapshot["stance"],
                "engagement_score": snapshot["engagement_score"],
            },
        )
        return True

    async def _expire_old_insights(self, session: Any, deal_id: UUID) -> int:
        """Expire old active insights for a deal before generating new ones.

        Args:
            session: Database session
            deal_id: UUID of the deal

        Returns:
            Number of insights expired
        """
        stmt = (
            update(DealStakeholderInsight)
            .where(
                col(DealStakeholderInsight.deal_id) == deal_id,
                col(DealStakeholderInsight.organisation_id) == self.organisation_id,
                col(DealStakeholderInsight.status) == StakeholderInsightStatus.ACTIVE,
            )
            .values(status=StakeholderInsightStatus.EXPIRED, updated_at=utcnow())
        )
        result = await session.execute(stmt)
        count = result.rowcount
        if count > 0:
            logger.info(
                "Expired %d old insights for deal %s",
                count,
                deal_id,
                extra={
                    "event": "stakeholder_analysis_old_insights_expired",
                    "organisation_id": str(self.organisation_id),
                    "deal_id": str(deal_id),
                    "expired_count": count,
                },
            )
        return count
