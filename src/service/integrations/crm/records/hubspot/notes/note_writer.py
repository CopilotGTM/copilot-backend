"""Write normalized HubSpot notes into the shared CRM activity table.

Called by both full note sync and single-record webhook/incremental refresh.
The `deactivate_missing` switch is the safety boundary: full snapshots may
soft-delete absent notes, but single-record refreshes must only upsert one row.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityType,
    CRMSourceObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.notes.note_types import (
    ActivityDimensionHit,
    NormalizedHubSpotNote,
    NoteWriteResult,
)

logger = get_logger(__name__)


class HubSpotNoteWriter:
    """Active-snapshot writer for HubSpot notes into crm_activities."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id
        self.source_mappings = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="hubspot",
        )

    async def apply(
        self,
        *,
        normalized: list[NormalizedHubSpotNote],
        all_external_ids: Iterable[str],
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
    ) -> NoteWriteResult:
        """Apply a full HubSpot note snapshot and soft-delete rows missing from it."""
        return await self._apply(
            normalized=normalized,
            all_external_ids=all_external_ids,
            user_map=user_map,
            deal_map=deal_map,
            contact_map=contact_map,
            company_map=company_map,
            deactivate_missing=True,
        )

    async def apply_single(
        self,
        *,
        note: NormalizedHubSpotNote,
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
    ) -> NoteWriteResult:
        """Upsert one note without treating absent notes as deleted."""
        return await self._apply(
            normalized=[note],
            all_external_ids=[note.external_id],
            user_map=user_map,
            deal_map=deal_map,
            contact_map=contact_map,
            company_map=company_map,
            deactivate_missing=False,
        )

    async def _apply(
        self,
        *,
        normalized: list[NormalizedHubSpotNote],
        all_external_ids: Iterable[str],
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
        deactivate_missing: bool,
    ) -> NoteWriteResult:
        result = NoteWriteResult()
        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows()

        for note in normalized:
            # Resolve associations before the write so missing dimensions are
            # counted but do not block note ingestion; later syncs can repair
            # the links once the related objects arrive.
            owner_hit = self._resolve_hit(
                note.owner_external_id, user_map, result, field_name="owner"
            )
            deal_hit = self._resolve_hit(note.deal_external_id, deal_map, result, field_name="deal")
            contact_hit = self._resolve_hit(
                note.contact_external_id, contact_map, result, field_name="contact"
            )
            company_hit = self._resolve_hit(
                note.company_external_id, company_map, result, field_name="company"
            )

            related_local_id, related_name = self._resolve_related_local(
                note=note,
                deal_hit=deal_hit,
                contact_hit=contact_hit,
                company_hit=company_hit,
            )

            existing = existing_by_external.get(note.external_id)
            if existing is None:
                row = self._new_row(
                    note=note,
                    owner_hit=owner_hit,
                    deal_hit=deal_hit,
                    contact_hit=contact_hit,
                    company_hit=company_hit,
                    related_local_id=related_local_id,
                    related_name=related_name,
                    now=now,
                )
                self.session.add(row)
                await self._upsert_source_mapping(note, row.activity_id, now=now)
                result.notes_created += 1
                continue

            if _is_stale_note(existing, note):
                existing.last_seen_at = now
                result.notes_skipped_stale += 1
                continue

            was_deleted = bool(existing.is_deleted)
            self._apply_updates(
                existing,
                note=note,
                owner_hit=owner_hit,
                deal_hit=deal_hit,
                contact_hit=contact_hit,
                company_hit=company_hit,
                related_local_id=related_local_id,
                related_name=related_name,
                now=now,
            )
            if was_deleted:
                result.notes_reactivated += 1
            else:
                result.notes_updated += 1
            await self._upsert_source_mapping(note, existing.activity_id, now=now)

        if deactivate_missing:
            # Only full sync owns tombstoning. Incremental and webhook refreshes
            # pass one object at a time, so using their payload as a complete
            # snapshot would incorrectly delete every unrelated note.
            snapshot_external_ids = set(all_external_ids)
            for external_id, row in existing_by_external.items():
                if external_id in snapshot_external_ids:
                    continue
                if not row.is_deleted:
                    row.is_deleted = True
                    result.notes_deactivated += 1
            await self.source_mappings.mark_missing_deleted(
                object_type=CRMSourceObjectType.NOTE,
                seen_external_ids=snapshot_external_ids,
            )

        await self.session.flush()
        logger.info(
            "HubSpot note writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "notes_created": result.notes_created,
                "notes_updated": result.notes_updated,
                "notes_reactivated": result.notes_reactivated,
                "notes_deactivated": result.notes_deactivated,
                "notes_skipped_stale": result.notes_skipped_stale,
                "unresolved_owner_link": result.unresolved_owner_link,
                "unresolved_deal_link": result.unresolved_deal_link,
                "unresolved_contact_link": result.unresolved_contact_link,
                "unresolved_company_link": result.unresolved_company_link,
            },
        )
        return result

    def _resolve_hit(
        self,
        external_id: str | None,
        mapping: dict[str, ActivityDimensionHit],
        result: NoteWriteResult,
        *,
        field_name: str,
    ) -> ActivityDimensionHit | None:
        if not external_id:
            return None
        resolved = mapping.get(external_id)
        if resolved is not None:
            return resolved
        if field_name == "owner":
            result.unresolved_owner_link += 1
        elif field_name == "deal":
            result.unresolved_deal_link += 1
        elif field_name == "contact":
            result.unresolved_contact_link += 1
        elif field_name == "company":
            result.unresolved_company_link += 1
        return None

    def _resolve_related_local(
        self,
        *,
        note: NormalizedHubSpotNote,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
    ) -> tuple[UUID | None, str | None]:
        if note.related_object_type == "DEAL" and deal_hit is not None:
            return deal_hit.internal_id, deal_hit.name
        if note.related_object_type == "CONTACT" and contact_hit is not None:
            return contact_hit.internal_id, contact_hit.name
        if note.related_object_type == "COMPANY" and company_hit is not None:
            return company_hit.internal_id, company_hit.name
        return None, None

    async def _load_existing_rows(self) -> dict[str, CRMActivity]:
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.NOTE,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMActivity).where(
            col(CRMActivity.organisation_id) == self.organisation_id,
            col(CRMActivity.integration_id) == self.integration_id,
            col(CRMActivity.activity_type) == CRMActivityType.NOTE.value,
            col(CRMActivity.activity_id).in_(set(external_to_local.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.activity_id]: row
            for row in rows
            if row.activity_id in local_to_external
        }

    async def _upsert_source_mapping(
        self,
        note: NormalizedHubSpotNote,
        local_activity_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
            external_object_type=CRMSourceObjectType.NOTE,
            external_id=note.external_id,
            local_object_type=CRMSourceObjectType.ACTIVITY,
            local_object_id=local_activity_id,
            data_payload=note.data_payload,
            source_created_at=note.source_created_at,
            source_modified_at=note.source_modified_at,
            synced_at=now,
        )

    def _new_row(
        self,
        *,
        note: NormalizedHubSpotNote,
        owner_hit: ActivityDimensionHit | None,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
        related_local_id: UUID | None,
        related_name: str | None,
        now: datetime,
    ) -> CRMActivity:
        return CRMActivity(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            related_object_type=note.related_object_type,
            related_local_id=related_local_id,
            related_name=related_name,
            deal_id=deal_hit.internal_id if deal_hit is not None else None,
            contact_id=contact_hit.internal_id if contact_hit is not None else None,
            company_id=company_hit.internal_id if company_hit is not None else None,
            subject=note.subject,
            body=note.body,
            status=None,
            activity_type=CRMActivityType.NOTE.value,
            activity_subtype=note.activity_subtype,
            priority=None,
            owner_name=owner_hit.name if owner_hit is not None else None,
            owner_email=owner_hit.email if owner_hit is not None else None,
            owner_id=owner_hit.internal_id if owner_hit is not None else None,
            source="CRM_SYNC",
            source_created_at=note.source_created_at,
            source_modified_at=note.source_modified_at,
            synced_at=now,
            last_seen_at=now,
            is_deleted=False,
            properties=note.properties,
            metrics={},
        )

    def _apply_updates(
        self,
        row: CRMActivity,
        *,
        note: NormalizedHubSpotNote,
        owner_hit: ActivityDimensionHit | None,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
        related_local_id: UUID | None,
        related_name: str | None,
        now: datetime,
    ) -> None:
        row.related_object_type = note.related_object_type
        row.related_local_id = related_local_id
        row.related_name = related_name
        row.deal_id = deal_hit.internal_id if deal_hit is not None else None
        row.contact_id = contact_hit.internal_id if contact_hit is not None else None
        row.company_id = company_hit.internal_id if company_hit is not None else None
        row.subject = note.subject
        row.body = note.body
        row.status = None
        row.activity_type = CRMActivityType.NOTE.value
        row.activity_subtype = note.activity_subtype
        row.priority = None
        row.owner_name = owner_hit.name if owner_hit is not None else None
        row.owner_email = owner_hit.email if owner_hit is not None else None
        row.owner_id = owner_hit.internal_id if owner_hit is not None else None
        row.source = "CRM_SYNC"
        row.source_created_at = note.source_created_at
        row.source_modified_at = note.source_modified_at
        row.synced_at = now
        row.last_seen_at = now
        row.is_deleted = False
        row.properties = note.properties
        row.metrics = {}


def _is_stale_note(row: CRMActivity, note: NormalizedHubSpotNote) -> bool:
    """Return whether a provider snapshot is older than the local active row."""
    if row.source_modified_at is None or note.source_modified_at is None:
        return False
    if bool(row.is_deleted):
        return False
    return row.source_modified_at >= note.source_modified_at
