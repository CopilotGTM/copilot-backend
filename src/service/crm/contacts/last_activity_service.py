"""Maintain stored Contact communication and activity timestamps.

Activity projection calls this service whenever Contact timeline membership
changes. Provider payloads remain fallbacks, so HubSpot and Salesforce activity
is preserved even when it has not reached the unified CopilotGTM timeline.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityLink,
    CRMActivityObjectType,
    CRMSourceKind,
    CRMSourceObjectMapping,
    CRMSourceObjectType,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact

CONTACTED_SOURCE_TYPES = frozenset({"EMAIL", "MEETING"})
ACTIVITY_SOURCE_TYPES = CONTACTED_SOURCE_TYPES | {"NOTE"}


class ContactLastActivityService:
    """Recompute one org-scoped Contact from timeline and provider signals."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def refresh_contact(self, contact_id: UUID) -> CRMContact | None:
        """Persist the effective timestamps for one active Contact."""
        contact = await self._load_contact(contact_id)
        if contact is None:
            return None

        timeline_contacted_at, timeline_activity_at = await self._latest_timeline_values(contact_id)
        provider_contacted_at, provider_activity_at = await self._latest_provider_values(contact_id)
        contact.last_contacted_at = _newest_timestamp(
            timeline_contacted_at,
            provider_contacted_at,
        )
        contact.last_activity_at = _newest_timestamp(
            timeline_activity_at,
            provider_activity_at,
        )
        self.session.add(contact)
        return contact

    async def _load_contact(self, contact_id: UUID) -> CRMContact | None:
        contact_c = cast(Any, CRMContact).__table__.c
        # The org predicate is the tenant boundary; a caller-supplied UUID alone
        # must never resolve a Contact from another organisation.
        stmt = select(CRMContact).where(
            contact_c.organisation_id == self.organisation_id,
            contact_c.contact_id == contact_id,
            contact_c.is_deleted.is_(False),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _latest_timeline_values(
        self,
        contact_id: UUID,
    ) -> tuple[datetime | None, datetime | None]:
        link = cast(Any, CRMActivityLink).__table__.c
        activity = cast(Any, CRMActivity).__table__.c
        contacted_at = func.max(
            case(
                (
                    activity.source_type.in_(CONTACTED_SOURCE_TYPES),
                    link.occurred_at,
                )
            )
        )
        activity_at = func.max(
            case(
                (
                    activity.source_type.in_(ACTIVITY_SOURCE_TYPES),
                    link.occurred_at,
                )
            )
        )
        stmt = (
            select(contacted_at, activity_at)
            .select_from(CRMActivityLink)
            .join(
                CRMActivity,
                (activity.organisation_id == link.organisation_id)
                & (activity.activity_id == link.activity_id),
            )
            .where(
                link.organisation_id == self.organisation_id,
                link.object_type == CRMActivityObjectType.CONTACT.value,
                link.object_id == contact_id,
                link.occurred_at <= func.now(),
                activity.is_deleted.is_(False),
                activity.source_type.in_(ACTIVITY_SOURCE_TYPES),
            )
        )
        row = (await self.session.execute(stmt)).one()
        return row[0], row[1]

    async def _latest_provider_values(
        self,
        contact_id: UUID,
    ) -> tuple[datetime | None, datetime | None]:
        mapping_c = cast(Any, CRMSourceObjectMapping).__table__.c
        # Source mappings are polymorphic, so both the org and local object type
        # are required before trusting the Contact UUID.
        stmt = (
            select(CRMSourceObjectMapping)
            .where(
                mapping_c.organisation_id == self.organisation_id,
                mapping_c.source_kind == CRMSourceKind.INTEGRATION.value,
                mapping_c.local_object_type == CRMSourceObjectType.CONTACT.value,
                mapping_c.local_object_id == contact_id,
                mapping_c.is_deleted.is_(False),
            )
            .order_by(col(CRMSourceObjectMapping.synced_at).desc())
            .limit(1)
        )
        mapping = (await self.session.execute(stmt)).scalar_one_or_none()
        if mapping is None:
            return None, None
        return _provider_values(str(mapping.provider).strip().lower(), mapping.data_payload)


def _provider_values(
    provider: str,
    payload: dict[str, Any] | None,
) -> tuple[datetime | None, datetime | None]:
    """Extract the provider timestamps supported by the Contact sync contracts."""
    if not isinstance(payload, dict):
        return None, None
    if provider == "salesforce":
        return None, _parse_provider_datetime(payload.get("LastActivityDate"))
    if provider == "hubspot":
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            return None, None
        return (
            _parse_provider_datetime(properties.get("notes_last_contacted")),
            _parse_provider_datetime(
                properties.get("notes_last_updated") or properties.get("lastactivitydate")
            ),
        )
    return None, None


def _newest_timestamp(*values: datetime | None) -> datetime | None:
    candidates = [_as_utc(value) for value in values if value is not None]
    return max(candidates) if candidates else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_provider_datetime(value: Any) -> datetime | None:
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
    return _as_utc(parsed)
