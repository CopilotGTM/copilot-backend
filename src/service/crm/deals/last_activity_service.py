"""Maintain one explainable freshness timestamp for every CRM deal.

Activity projection, deal mutations, provider sync, and the maintenance
backfill all call this service. CopilotGTM activity follows the central
allowlist, while Salesforce and HubSpot timestamps remain a fallback when the
provider knows about activity that has not reached our unified timeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMSourceKind,
    CRMSourceObjectMapping,
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealMutationEvent

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


@dataclass(slots=True, frozen=True)
class DealLastActivityResult:
    """The effective deal timestamp and the system that supplied it."""

    occurred_at: datetime | None
    source: str | None


class DealLastActivityService:
    """Recompute deal freshness from CopilotGTM and provider activity signals."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def refresh_deal(self, deal_id: UUID) -> datetime | None:
        """Persist the newest known activity for one org-scoped deal."""
        deal = await self._load_deal(deal_id)
        if deal is None:
            return None

        result = await self.compute_deal_with_source(deal_id)
        deal.last_activity_at = result.occurred_at
        properties = dict(deal.properties or {})
        if result.source is None:
            properties.pop("last_activity_source", None)
        else:
            properties["last_activity_source"] = result.source
        deal.properties = properties
        self.session.add(deal)
        return result.occurred_at

    async def compute_deal(self, deal_id: UUID) -> datetime | None:
        """Return the effective timestamp without changing the deal row."""
        return (await self.compute_deal_with_source(deal_id)).occurred_at

    async def compute_deal_with_source(self, deal_id: UUID) -> DealLastActivityResult:
        """Return the effective timestamp and its CopilotGTM/provider provenance."""
        activity_at = await self._latest_activity_link_at(deal_id)
        mutation_at = await self._latest_mutation_at(deal_id)
        provider_at, provider = await self._latest_provider_activity(deal_id)
        copilot_candidates = [value for value in (activity_at, mutation_at) if value is not None]
        copilot_at = max(copilot_candidates) if copilot_candidates else None

        # CopilotGTM wins ties because its matching timeline row makes the date
        # directly explainable to the user.
        if copilot_at is not None and (provider_at is None or copilot_at >= provider_at):
            return DealLastActivityResult(copilot_at, "copilot")
        if provider_at is not None:
            return DealLastActivityResult(provider_at, provider)
        return DealLastActivityResult(None, None)

    async def _load_deal(self, deal_id: UUID) -> CRMDeal | None:
        deal_c = cast(Any, CRMDeal).__table__.c
        # The route-supplied deal UUID is not a tenant boundary by itself.
        stmt = select(CRMDeal).where(
            deal_c.organisation_id == self.organisation_id,
            deal_c.deal_id == deal_id,
            deal_c.is_deleted.is_(False),
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

    async def _latest_provider_activity(self, deal_id: UUID) -> tuple[datetime | None, str | None]:
        """Read the provider fallback from the existing raw source payload."""
        mapping_c = cast(Any, CRMSourceObjectMapping).__table__.c
        # Source mappings are polymorphic, so both org and local object type
        # are required before trusting the caller-supplied deal UUID.
        stmt = (
            select(CRMSourceObjectMapping)
            .where(
                mapping_c.organisation_id == self.organisation_id,
                mapping_c.source_kind == CRMSourceKind.INTEGRATION.value,
                mapping_c.local_object_type == CRMSourceObjectType.DEAL.value,
                mapping_c.local_object_id == deal_id,
                mapping_c.is_deleted.is_(False),
            )
            .order_by(col(CRMSourceObjectMapping.synced_at).desc())
            .limit(1)
        )
        mapping = (await self.session.execute(stmt)).scalar_one_or_none()
        if mapping is None:
            return None, None
        provider = str(mapping.provider).strip().lower()
        return _provider_activity_at(provider, mapping.data_payload), provider


def is_last_activity_mutation(semantic_event_types: list[str] | None) -> bool:
    """Return whether a mutation should refresh deal freshness."""
    return bool(set(semantic_event_types or []).intersection(ALLOWED_MUTATION_SEMANTICS))


def _provider_activity_at(provider: str, payload: dict[str, Any] | None) -> datetime | None:
    """Extract a supported CRM's last-activity timestamp from its raw payload."""
    if not isinstance(payload, dict):
        return None
    if provider == "salesforce":
        return _parse_provider_datetime(payload.get("LastActivityDate"))
    if provider == "hubspot":
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            return None
        return _parse_provider_datetime(
            properties.get("notes_last_updated") or properties.get("lastactivitydate")
        )
    return None


def _parse_provider_datetime(value: Any) -> datetime | None:
    """Parse provider ISO timestamps and normalize date-only values to UTC."""
    if not isinstance(value, str) or not value:
        return None
    if len(value) == 10:
        try:
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=UTC)
        except ValueError:
            return None
    cleaned = value.replace("Z", "+00:00")
    if len(cleaned) >= 5 and cleaned[-5] in {"+", "-"} and cleaned[-3] != ":":
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
