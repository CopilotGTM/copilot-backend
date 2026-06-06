"""Write normalized HubSpot tasks into the shared CRM activity table.

Called by both full task sync and single-record webhook/incremental refresh.
The `deactivate_missing` switch is the safety boundary: full snapshots may
soft-delete absent tasks, but single-record refreshes must only upsert one row.
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
from src.service.integrations.crm.records.hubspot.tasks.task_types import (
    ActivityDimensionHit,
    NormalizedHubSpotTask,
    TaskWriteResult,
)

logger = get_logger(__name__)


class HubSpotTaskWriter:
    """Active-snapshot writer for HubSpot tasks into crm_activities."""

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
        normalized: list[NormalizedHubSpotTask],
        all_external_ids: Iterable[str],
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
    ) -> TaskWriteResult:
        """Apply a full HubSpot task snapshot and soft-delete rows missing from it."""
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
        task: NormalizedHubSpotTask,
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
    ) -> TaskWriteResult:
        """Upsert one task without treating absent tasks as deleted."""
        return await self._apply(
            normalized=[task],
            all_external_ids=[task.external_id],
            user_map=user_map,
            deal_map=deal_map,
            contact_map=contact_map,
            company_map=company_map,
            deactivate_missing=False,
        )

    async def _apply(
        self,
        *,
        normalized: list[NormalizedHubSpotTask],
        all_external_ids: Iterable[str],
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
        deactivate_missing: bool,
    ) -> TaskWriteResult:
        result = TaskWriteResult()
        now = datetime.now(UTC)
        existing_by_external = await self._load_existing_rows()

        for task in normalized:
            # Resolve associations before the write so missing dimensions are
            # counted but do not block task ingestion; later syncs can repair
            # the links once the related objects arrive.
            owner_hit = self._resolve_hit(
                task.owner_external_id,
                user_map,
                result,
                field_name="owner",
            )
            deal_hit = self._resolve_hit(
                task.deal_external_id,
                deal_map,
                result,
                field_name="deal",
            )
            contact_hit = self._resolve_hit(
                task.contact_external_id,
                contact_map,
                result,
                field_name="contact",
            )
            company_hit = self._resolve_hit(
                task.company_external_id,
                company_map,
                result,
                field_name="company",
            )

            related_local_id, related_name = self._resolve_related_local(
                task=task,
                deal_hit=deal_hit,
                contact_hit=contact_hit,
                company_hit=company_hit,
            )

            existing = existing_by_external.get(task.external_id)
            if existing is None:
                row = self._new_row(
                    task=task,
                    owner_hit=owner_hit,
                    deal_hit=deal_hit,
                    contact_hit=contact_hit,
                    company_hit=company_hit,
                    related_local_id=related_local_id,
                    related_name=related_name,
                    now=now,
                )
                self.session.add(row)
                await self._upsert_source_mapping(task, row.activity_id, now=now)
                result.tasks_created += 1
                continue

            if _is_stale_task(existing, task):
                existing.last_seen_at = now
                result.tasks_skipped_stale += 1
                continue

            was_deleted = bool(existing.is_deleted)
            self._apply_updates(
                existing,
                task=task,
                owner_hit=owner_hit,
                deal_hit=deal_hit,
                contact_hit=contact_hit,
                company_hit=company_hit,
                related_local_id=related_local_id,
                related_name=related_name,
                now=now,
            )
            if was_deleted:
                result.tasks_reactivated += 1
            else:
                result.tasks_updated += 1
            await self._upsert_source_mapping(task, existing.activity_id, now=now)

        if deactivate_missing:
            # Only full sync owns tombstoning. Incremental and webhook refreshes
            # pass one object at a time, so using their payload as a complete
            # snapshot would incorrectly delete every unrelated task.
            snapshot_external_ids = set(all_external_ids)
            for external_id, row in existing_by_external.items():
                if external_id in snapshot_external_ids:
                    continue
                if not row.is_deleted:
                    row.is_deleted = True
                    result.tasks_deactivated += 1
            await self.source_mappings.mark_missing_deleted(
                object_type=CRMSourceObjectType.TASK,
                seen_external_ids=snapshot_external_ids,
            )

        await self.session.flush()
        logger.info(
            "HubSpot task writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "tasks_created": result.tasks_created,
                "tasks_updated": result.tasks_updated,
                "tasks_reactivated": result.tasks_reactivated,
                "tasks_deactivated": result.tasks_deactivated,
                "tasks_skipped_stale": result.tasks_skipped_stale,
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
        result: TaskWriteResult,
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
        task: NormalizedHubSpotTask,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
    ) -> tuple[UUID | None, str | None]:
        if task.related_object_type == "DEAL" and deal_hit is not None:
            return deal_hit.internal_id, deal_hit.name
        if task.related_object_type == "CONTACT" and contact_hit is not None:
            return contact_hit.internal_id, contact_hit.name
        if task.related_object_type == "COMPANY" and company_hit is not None:
            return company_hit.internal_id, company_hit.name
        return None, None

    async def _load_existing_rows(self) -> dict[str, CRMActivity]:
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.TASK,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMActivity).where(
            col(CRMActivity.organisation_id) == self.organisation_id,
            col(CRMActivity.integration_id) == self.integration_id,
            col(CRMActivity.activity_type).in_(
                [CRMActivityType.TASK.value, CRMActivityType.TODO.value]
            ),
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
        task: NormalizedHubSpotTask,
        local_activity_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
            external_object_type=CRMSourceObjectType.TASK,
            external_id=task.external_id,
            local_object_type=CRMSourceObjectType.ACTIVITY,
            local_object_id=local_activity_id,
            data_payload=task.data_payload,
            source_created_at=task.source_created_at,
            source_modified_at=task.source_modified_at,
            synced_at=now,
        )

    def _new_row(
        self,
        *,
        task: NormalizedHubSpotTask,
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
            related_object_type=task.related_object_type,
            related_local_id=related_local_id,
            related_name=related_name,
            deal_id=deal_hit.internal_id if deal_hit is not None else None,
            contact_id=contact_hit.internal_id if contact_hit is not None else None,
            company_id=company_hit.internal_id if company_hit is not None else None,
            subject=task.subject,
            body=task.body,
            status=task.status,
            activity_type=CRMActivityType.TASK.value,
            activity_subtype=task.activity_subtype,
            priority=task.priority,
            owner_name=owner_hit.name if owner_hit is not None else None,
            owner_email=owner_hit.email if owner_hit is not None else None,
            owner_id=owner_hit.internal_id if owner_hit is not None else None,
            source="CRM_SYNC",
            due_date=task.due_date,
            completed_date=task.completed_date,
            source_created_at=task.source_created_at,
            source_modified_at=task.source_modified_at,
            synced_at=now,
            last_seen_at=now,
            is_deleted=False,
            properties=task.properties,
            metrics={},
        )

    def _apply_updates(
        self,
        row: CRMActivity,
        *,
        task: NormalizedHubSpotTask,
        owner_hit: ActivityDimensionHit | None,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
        related_local_id: UUID | None,
        related_name: str | None,
        now: datetime,
    ) -> None:
        row.related_object_type = task.related_object_type
        row.related_local_id = related_local_id
        row.related_name = related_name
        row.deal_id = deal_hit.internal_id if deal_hit is not None else None
        row.contact_id = contact_hit.internal_id if contact_hit is not None else None
        row.company_id = company_hit.internal_id if company_hit is not None else None
        row.subject = task.subject
        row.body = task.body
        row.status = task.status
        row.activity_type = CRMActivityType.TASK.value
        row.activity_subtype = task.activity_subtype
        row.priority = task.priority
        row.owner_name = owner_hit.name if owner_hit is not None else None
        row.owner_email = owner_hit.email if owner_hit is not None else None
        row.owner_id = owner_hit.internal_id if owner_hit is not None else None
        row.source = "CRM_SYNC"
        row.due_date = task.due_date
        row.completed_date = task.completed_date
        row.source_created_at = task.source_created_at
        row.source_modified_at = task.source_modified_at
        row.synced_at = now
        row.last_seen_at = now
        row.is_deleted = False
        row.properties = task.properties
        row.metrics = {}


def _is_stale_task(row: CRMActivity, task: NormalizedHubSpotTask) -> bool:
    """Return whether a provider snapshot is older than the local active row."""
    if row.source_modified_at is None or task.source_modified_at is None:
        return False
    if bool(row.is_deleted):
        return False
    return row.source_modified_at >= task.source_modified_at
