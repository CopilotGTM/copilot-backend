from __future__ import annotations

from collections.abc import Set
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import dialect as pg_dialect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import CRMActivity, CRMActivityType
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import (
    CRMSourceMappingHelper,
    IntegrationMappingUpsert,
)
from src.service.integrations.crm.records.salesforce.notes.note_types import (
    ActivityDimensionHit,
    NormalizedSalesforceNote,
    NoteWriteResult,
)

logger = get_logger(__name__)

_ASYNC_PG_BIND_PARAMETER_LIMIT = 32_767
_BULK_UPSERT_PARAMETER_BUDGET = 24_000


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
        self.source_mappings = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="salesforce",
        )
        self._activity_ids_by_external: dict[str, UUID] = {}

    async def apply(
        self,
        *,
        normalized: list[NormalizedSalesforceNote],
        snapshot_started_at: datetime | None,
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
        deactivate_missing: bool = True,
        deactivate_deal_ids: Set[UUID] | None = None,
    ) -> NoteWriteResult:
        result = NoteWriteResult()
        now = datetime.now(UTC)
        snapshot_time = snapshot_started_at or now
        normalized_by_external = {note.external_id: note for note in normalized}
        batch = list(normalized_by_external.values())
        batch_external_ids = set(normalized_by_external)
        existing_by_external = await self._load_existing_rows(external_ids=batch_external_ids)
        rows: list[dict] = []
        mapping_rows: list[IntegrationMappingUpsert] = []

        for note in batch:
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
                result.notes_created += 1
            elif bool(existing.is_deleted):
                result.notes_reactivated += 1
            else:
                result.notes_updated += 1

            activity_id = (
                existing.activity_id
                if existing is not None
                else self._activity_ids_by_external.get(note.external_id, uuid4())
            )
            if existing is not None:
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
                    last_seen_at=snapshot_time,
                )

            rows.append(
                self._row_values(
                    activity_id=activity_id,
                    note=note,
                    owner_hit=owner_hit,
                    deal_hit=deal_hit,
                    contact_hit=contact_hit,
                    company_hit=company_hit,
                    related_local_id=related_local_id,
                    related_name=related_name,
                    now=now,
                    last_seen_at=snapshot_time,
                )
            )
            self._activity_ids_by_external[note.external_id] = activity_id
            mapping_rows.extend(
                self._source_mapping_rows(
                    note=note,
                    local_activity_id=activity_id,
                    now=now,
                )
            )

        if rows:
            await self._bulk_upsert(rows)
            await self.source_mappings.bulk_upsert_integration_mappings(mapping_rows)

        if deactivate_missing:
            result.notes_deactivated += await self._deactivate_missing(
                snapshot_time,
                deal_ids=deactivate_deal_ids,
            )

        await self.session.flush()
        logger.info(
            "Salesforce note writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "batch_size": len(batch),
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

    async def _bulk_upsert(self, rows: list[dict]) -> None:
        if not rows:
            return
        max_rows_per_statement = max(1, _BULK_UPSERT_PARAMETER_BUDGET // len(rows[0]))
        for offset in range(0, len(rows), max_rows_per_statement):
            await self._bulk_upsert_chunk(rows[offset : offset + max_rows_per_statement])

    async def _bulk_upsert_chunk(self, rows: list[dict]) -> None:
        insert_stmt = pg_insert(CRMActivity).values(rows)
        excluded = insert_stmt.excluded
        update_columns = {
            column_name: getattr(excluded, column_name)
            for column_name in rows[0]
            if column_name not in {"activity_id", "created_at"}
        }
        update_columns["updated_at"] = datetime.now(UTC)
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["activity_id"],
            set_=update_columns,
        )
        await self.session.execute(stmt)

    async def _deactivate_missing(
        self,
        snapshot_started_at: datetime,
        *,
        deal_ids: Set[UUID] | None = None,
    ) -> int:
        if deal_ids is not None and not deal_ids:
            return 0

        stmt = update(CRMActivity).where(
            col(CRMActivity.organisation_id) == self.organisation_id,
            col(CRMActivity.integration_id) == self.integration_id,
            col(CRMActivity.activity_type) == CRMActivityType.NOTE.value,
            col(CRMActivity.is_deleted) == False,  # noqa: E712
            or_(
                col(CRMActivity.last_seen_at).is_(None),
                col(CRMActivity.last_seen_at) < snapshot_started_at,
            ),
        )
        if deal_ids is not None:
            stmt = stmt.where(col(CRMActivity.deal_id).in_(deal_ids))
        stmt = stmt.values(is_deleted=True, updated_at=datetime.now(UTC))
        result = await self.session.execute(stmt)
        rowcount = getattr(result, "rowcount", 0)
        return rowcount if isinstance(rowcount, int) else 0

    def _pg_dialect(self):
        return pg_dialect()

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

    async def _load_existing_rows(
        self,
        *,
        external_ids: Set[str] | None = None,
    ) -> dict[str, CRMActivity]:
        self._activity_ids_by_external = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.NOTE,
            external_ids=external_ids,
            include_deleted=True,
        )
        if not self._activity_ids_by_external:
            return {}
        stmt = select(CRMActivity).where(
            col(CRMActivity.organisation_id) == self.organisation_id,
            col(CRMActivity.integration_id) == self.integration_id,
            col(CRMActivity.activity_type) == CRMActivityType.NOTE.value,
            col(CRMActivity.activity_id).in_(set(self._activity_ids_by_external.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id
            for external_id, local_id in self._activity_ids_by_external.items()
        }
        return {
            local_to_external[row.activity_id]: row
            for row in rows
            if row.activity_id in local_to_external
        }

    def _source_mapping_rows(
        self,
        *,
        note: NormalizedSalesforceNote,
        local_activity_id: UUID,
        now: datetime,
    ) -> list[IntegrationMappingUpsert]:
        rows = [
            IntegrationMappingUpsert(
                external_object_type=CRMSourceObjectType.NOTE,
                external_id=note.external_id,
                local_object_type=CRMSourceObjectType.ACTIVITY,
                local_object_id=local_activity_id,
                data_payload=note.data_payload,
                source_created_at=note.source_created_at,
                source_modified_at=note.source_modified_at,
                synced_at=now,
            )
        ]
        rows.extend(
            IntegrationMappingUpsert(
                external_object_type=CRMSourceObjectType.NOTE_LINK,
                external_id=link_id,
                local_object_type=CRMSourceObjectType.ACTIVITY,
                local_object_id=local_activity_id,
                data_payload=note.data_payload,
                source_created_at=note.source_created_at,
                source_modified_at=note.source_modified_at,
                synced_at=now,
            )
            for link_id in _iter_content_document_link_ids(note.data_payload)
        )
        return rows

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

    def _row_values(
        self,
        *,
        activity_id: UUID | None = None,
        note: NormalizedSalesforceNote,
        owner_hit: ActivityDimensionHit | None,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
        related_local_id: UUID | None,
        related_name: str | None,
        now: datetime,
        last_seen_at: datetime,
    ) -> dict:
        return {
            "organisation_id": self.organisation_id,
            "integration_id": self.integration_id,
            "activity_id": activity_id or uuid4(),
            "related_object_type": note.related_object_type,
            "related_local_id": related_local_id,
            "related_name": related_name,
            "deal_id": deal_hit.internal_id if deal_hit is not None else None,
            "contact_id": contact_hit.internal_id if contact_hit is not None else None,
            "company_id": company_hit.internal_id if company_hit is not None else None,
            "subject": note.subject,
            "body": note.body,
            "status": None,
            "activity_type": CRMActivityType.NOTE.value,
            "activity_subtype": note.activity_subtype,
            "priority": None,
            "owner_name": owner_hit.name if owner_hit is not None else None,
            "owner_email": owner_hit.email if owner_hit is not None else None,
            "owner_id": owner_hit.internal_id if owner_hit is not None else None,
            "source": "CRM_SYNC",
            "source_created_at": note.source_created_at,
            "source_modified_at": note.source_modified_at,
            "synced_at": now,
            "last_seen_at": last_seen_at,
            "is_deleted": False,
            "properties": note.properties,
            "metrics": {},
        }

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
        last_seen_at: datetime | None = None,
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
        row.last_seen_at = last_seen_at or now
        row.is_deleted = False
        row.properties = note.properties
        row.metrics = {}


def _iter_content_document_link_ids(data_payload: dict) -> list[str]:
    rows = data_payload.get("rows") if isinstance(data_payload, dict) else None
    if not isinstance(rows, list):
        return []
    link_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        link_id = row.get("ContentDocumentLinkId") or row.get("Id")
        if isinstance(link_id, str) and link_id.strip():
            link_ids.append(link_id.strip())
    return link_ids
