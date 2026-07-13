"""Maintain the user-facing freshness timestamp for internal CRM deals."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityLink
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealMutationEvent
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)

ALLOWED_ACTIVITY_SOURCE_TYPES = frozenset(
    {
        "DEAL_CREATED",
        "EMAIL",
        "MEETING",
        "NOTE",
        "STAGE_CHANGE",
        "STAKEHOLDER_ADDED",
        "STAKEHOLDER_REMOVED",
        "STAKEHOLDER_UPDATED",
    }
)

ALLOWED_MUTATION_SEMANTICS = frozenset(
    {
        "crm.deal.amount_changed",
        "crm.deal.expected_close_date_changed",
        "crm.deal.owner_changed",
    }
)


class DealLastActivityService:
    """Recompute `CRMDeal.last_activity_at` from the central deal-activity policy."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def refresh_deal(self, deal_id: UUID) -> datetime | None:
        """Set one internal deal's last activity to the newest allowed activity."""
        deal = await self._load_internal_deal(deal_id)
        if deal is None:
            return None

        last_activity_at = await self.compute_deal(deal_id)
        deal.last_activity_at = last_activity_at
        self.session.add(deal)
        return last_activity_at

    async def compute_deal(self, deal_id: UUID) -> datetime | None:
        """Return the newest timestamp from allowed feed rows and mutation events."""
        activity_at = await self._latest_activity_link_at(deal_id)
        mutation_at = await self._latest_mutation_at(deal_id)
        candidates = [value for value in (activity_at, mutation_at) if value is not None]
        return max(candidates) if candidates else None

    async def _load_internal_deal(self, deal_id: UUID) -> CRMDeal | None:
        deal_c = cast(Any, CRMDeal).__table__.c
        integration_c = cast(Any, KnowledgeIntegration).__table__.c
        stmt = (
            select(CRMDeal)
            .outerjoin(
                KnowledgeIntegration,
                and_(
                    integration_c.organisation_id == deal_c.organisation_id,
                    integration_c.integration_id == deal_c.integration_id,
                ),
            )
            .where(
                deal_c.organisation_id == self.organisation_id,
                deal_c.deal_id == deal_id,
                deal_c.is_deleted.is_(False),
                or_(
                    deal_c.properties["is_internal_crm"].as_boolean().is_(True),
                    integration_c.integration_type == IntegrationType.INTERNAL_CRM.value,
                ),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _latest_activity_link_at(self, deal_id: UUID) -> datetime | None:
        link = cast(Any, CRMActivityLink).__table__.c
        activity = cast(Any, CRMActivity).__table__.c
        stmt = (
            select(func.max(link.occurred_at))
            .select_from(CRMActivityLink)
            .join(
                CRMActivity,
                (activity.organisation_id == link.organisation_id)
                & (activity.activity_id == link.activity_id),
            )
            .where(
                link.organisation_id == self.organisation_id,
                link.object_type == "DEAL",
                link.object_id == deal_id,
                link.occurred_at <= func.now(),
                activity.is_deleted.is_(False),
                activity.source_type.in_(ALLOWED_ACTIVITY_SOURCE_TYPES),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _latest_mutation_at(self, deal_id: UUID) -> datetime | None:
        event = cast(Any, CRMDealMutationEvent).__table__.c
        semantic_overlap = event.semantic_event_types.op("&&")(list(ALLOWED_MUTATION_SEMANTICS))
        stmt = select(func.max(event.occurred_at)).where(
            event.organisation_id == self.organisation_id,
            event.deal_id == deal_id,
            semantic_overlap,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


def is_last_activity_mutation(semantic_event_types: list[str] | None) -> bool:
    """Return whether a mutation should refresh deal freshness."""
    return bool(set(semantic_event_types or []).intersection(ALLOWED_MUTATION_SEMANTICS))
