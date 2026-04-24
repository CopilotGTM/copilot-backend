from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.notes.note_types import (
    ActivityDimensionHit,
    NormalizedSalesforceNote,
    NoteWriteResult,
)

logger = get_logger(__name__)


class SalesforceNoteWriter:
    """Active-snapshot writer for Salesforce notes into crm_activities."""

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

    async def apply(
        self,
        *,
        normalized: list[NormalizedSalesforceNote],
        all_external_ids: Iterable[str],
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
    ) -> NoteWriteResult:
        result = NoteWriteResult()
        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows()

        for note in normalized:
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
                result.notes_created += 1
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

        snapshot_external_ids = set(all_external_ids)
        for external_id, row in existing_by_external.items():
            if external_id in snapshot_external_ids:
                continue
            if not row.is_deleted:
                row.is_deleted = True
                result.notes_deactivated += 1

        await self.session.flush()
        logger.info(
            "Salesforce note writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "notes_created": result.notes_created,
                "notes_updated": result.notes_updated,
                "notes_reactivated": result.notes_reactivated,
                "notes_deactivated": result.notes_deactivated,
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
        note: NormalizedSalesforceNote,
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
        stmt = select(CRMActivity).where(
            col(CRMActivity.organisation_id) == self.organisation_id,
            col(CRMActivity.integration_id) == self.integration_id,
            col(CRMActivity.activity_type) == CRMActivityType.NOTE.value,
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.external_id: row for row in rows if row.external_id is not None}

    def _new_row(
        self,
        *,
        note: NormalizedSalesforceNote,
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
            external_id=note.external_id,
            related_object_type=note.related_object_type,
            related_external_id=note.related_external_id,
            related_local_id=related_local_id,
            related_name=related_name,
            deal_id=deal_hit.internal_id if deal_hit is not None else None,
            deal_external_id=note.deal_external_id,
            contact_id=contact_hit.internal_id if contact_hit is not None else None,
            contact_external_id=note.contact_external_id,
            company_id=company_hit.internal_id if company_hit is not None else None,
            company_external_id=note.company_external_id,
            subject=note.subject,
            body=note.body,
            status=None,
            activity_type=CRMActivityType.NOTE.value,
            activity_subtype=note.activity_subtype,
            priority=None,
            owner_external_id=note.owner_external_id,
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
            data_payload=note.data_payload,
        )

    def _apply_updates(
        self,
        row: CRMActivity,
        *,
        note: NormalizedSalesforceNote,
        owner_hit: ActivityDimensionHit | None,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
        related_local_id: UUID | None,
        related_name: str | None,
        now: datetime,
    ) -> None:
        row.related_object_type = note.related_object_type
        row.related_external_id = note.related_external_id
        row.related_local_id = related_local_id
        row.related_name = related_name
        row.deal_id = deal_hit.internal_id if deal_hit is not None else None
        row.deal_external_id = note.deal_external_id
        row.contact_id = contact_hit.internal_id if contact_hit is not None else None
        row.contact_external_id = note.contact_external_id
        row.company_id = company_hit.internal_id if company_hit is not None else None
        row.company_external_id = note.company_external_id
        row.subject = note.subject
        row.body = note.body
        row.status = None
        row.activity_type = CRMActivityType.NOTE.value
        row.activity_subtype = note.activity_subtype
        row.priority = None
        row.owner_external_id = note.owner_external_id
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
        row.data_payload = note.data_payload
