from __future__ import annotations

from collections.abc import Set
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import dialect as pg_dialect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import CRMTask
from src.domain.models.organisation_tables.sync.crm.source_mappings import (
    CRMSourceObjectType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.integrations.crm.records.common.query_chunks import (
    LOCAL_ID_QUERY_CHUNK_SIZE,
    iter_chunks,
)
from src.service.integrations.crm.records.common.source_mapping import (
    CRMSourceMappingHelper,
    IntegrationMappingUpsert,
)
from src.service.integrations.crm.records.salesforce.tasks.task_types import (
    ActivityDimensionHit,
    NormalizedSalesforceTask,
    TaskWriteResult,
)

logger = get_logger(__name__)

_ASYNC_PG_BIND_PARAMETER_LIMIT = 32_767
_BULK_UPSERT_PARAMETER_BUDGET = 24_000


class SalesforceTaskWriter:
    """Active-snapshot writer for Salesforce tasks into crm_tasks."""

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
        self._task_ids_by_external: dict[str, UUID] = {}
        self._projection = ActivityFeedProjectionService(session, organisation_id)

    async def apply(
        self,
        *,
        normalized: list[NormalizedSalesforceTask],
        snapshot_started_at: datetime | None,
        user_map: dict[str, ActivityDimensionHit],
        deal_map: dict[str, ActivityDimensionHit],
        contact_map: dict[str, ActivityDimensionHit],
        company_map: dict[str, ActivityDimensionHit],
        deactivate_missing: bool = True,
        deactivate_deal_ids: Set[UUID] | None = None,
    ) -> TaskWriteResult:
        result = TaskWriteResult()
        now = datetime.now(UTC)
        snapshot_time = snapshot_started_at or now
        normalized_by_external = {task.external_id: task for task in normalized}
        batch = list(normalized_by_external.values())
        batch_external_ids = set(normalized_by_external)
        existing_by_external = await self._load_existing_rows(external_ids=batch_external_ids)
        rows: list[dict] = []
        mapping_rows: list[IntegrationMappingUpsert] = []

        for task in batch:
            owner_hit = self._resolve_hit(
                task.owner_external_id, user_map, result, field_name="owner"
            )
            deal_hit = self._resolve_hit(task.deal_external_id, deal_map, result, field_name="deal")
            contact_hit = self._resolve_hit(
                task.contact_external_id, contact_map, result, field_name="contact"
            )
            company_hit = self._resolve_hit(
                task.company_external_id, company_map, result, field_name="company"
            )

            related_local_id, related_name = self._resolve_related_local(
                task=task,
                deal_hit=deal_hit,
                contact_hit=contact_hit,
                company_hit=company_hit,
            )

            existing = existing_by_external.get(task.external_id)
            if existing is None:
                result.tasks_created += 1
            elif bool(existing.is_deleted):
                result.tasks_reactivated += 1
            else:
                result.tasks_updated += 1

            task_id = (
                existing.task_id
                if existing is not None
                else self._task_ids_by_external.get(task.external_id, uuid4())
            )
            if existing is not None:
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
                    last_seen_at=snapshot_time,
                )

            rows.append(
                self._row_values(
                    task_id=task_id,
                    task=task,
                    owner_hit=owner_hit,
                    deal_hit=deal_hit,
                    contact_hit=contact_hit,
                    company_hit=company_hit,
                    related_local_id=related_local_id,
                    related_name=related_name,
                    last_seen_at=snapshot_time,
                )
            )
            self._task_ids_by_external[task.external_id] = task_id
            mapping_rows.append(
                self._source_mapping_row(
                    task=task,
                    local_task_id=task_id,
                    now=now,
                )
            )

        if rows:
            await self._bulk_upsert(rows)
            await self.source_mappings.bulk_upsert_integration_mappings(mapping_rows)
            for row in rows:
                await self._projection.upsert_task(CRMTask.model_validate(row))

        if deactivate_missing:
            result.tasks_deactivated += await self._deactivate_missing(
                snapshot_time,
                deal_ids=deactivate_deal_ids,
            )

        await self.session.flush()
        logger.info(
            "Salesforce task writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "batch_size": len(batch),
                "tasks_created": result.tasks_created,
                "tasks_updated": result.tasks_updated,
                "tasks_reactivated": result.tasks_reactivated,
                "tasks_deactivated": result.tasks_deactivated,
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
        insert_stmt = pg_insert(CRMTask).values(rows)
        excluded = insert_stmt.excluded
        update_columns = {
            column_name: getattr(excluded, column_name)
            for column_name in rows[0]
            if column_name not in {"task_id", "created_at"}
        }
        update_columns["updated_at"] = datetime.now(UTC)
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["task_id"],
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

        c = CRMTask.__table__.c
        base_conditions = [
            c.organisation_id == self.organisation_id,
            c.integration_id == self.integration_id,
            c.is_deleted == False,  # noqa: E712
            or_(
                c.task_metadata["last_seen_at"].as_string().is_(None),
                c.task_metadata["last_seen_at"].as_string() < snapshot_started_at.isoformat(),
            ),
        ]
        total = 0
        deal_id_batches = (
            [None] if deal_ids is None else iter_chunks(deal_ids, LOCAL_ID_QUERY_CHUNK_SIZE)
        )
        for deal_id_batch in deal_id_batches:
            conditions = list(base_conditions)
            if deal_id_batch is not None:
                conditions.append(c.deal_id.in_(deal_id_batch))
            task_ids = (
                (await self.session.execute(select(c.task_id).where(*conditions))).scalars().all()
            )
            if not task_ids:
                continue
            stmt = (
                update(CRMTask)
                .where(c.task_id.in_(task_ids))
                .values(is_deleted=True, updated_at=datetime.now(UTC))
            )
            result = await self.session.execute(stmt)
            rowcount = getattr(result, "rowcount", 0)
            total += rowcount if isinstance(rowcount, int) else 0
            for task_id in task_ids:
                await self._projection.soft_delete_activity_projection(task_id)
        return total

    def _pg_dialect(self):
        return pg_dialect()

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
        task: NormalizedSalesforceTask,
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

    async def _load_existing_rows(
        self,
        *,
        external_ids: Set[str] | None = None,
    ) -> dict[str, CRMTask]:
        self._task_ids_by_external = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.TASK,
            external_ids=external_ids,
            include_deleted=True,
        )
        if not self._task_ids_by_external:
            return {}
        rows: list[CRMTask] = []
        for task_ids in iter_chunks(
            set(self._task_ids_by_external.values()),
            LOCAL_ID_QUERY_CHUNK_SIZE,
        ):
            stmt = select(CRMTask).where(
                col(CRMTask.organisation_id) == self.organisation_id,
                col(CRMTask.integration_id) == self.integration_id,
                col(CRMTask.task_id).in_(task_ids),
            )
            rows.extend((await self.session.execute(stmt)).scalars().all())
        local_to_external = {
            local_id: external_id for external_id, local_id in self._task_ids_by_external.items()
        }
        return {
            local_to_external[row.task_id]: row for row in rows if row.task_id in local_to_external
        }

    def _source_mapping_row(
        self,
        *,
        task: NormalizedSalesforceTask,
        local_task_id: UUID,
        now: datetime,
    ) -> IntegrationMappingUpsert:
        return IntegrationMappingUpsert(
            external_object_type=CRMSourceObjectType.TASK,
            external_id=task.external_id,
            local_object_type=CRMSourceObjectType.TASK,
            local_object_id=local_task_id,
            data_payload=task.data_payload,
            source_created_at=task.source_created_at,
            source_modified_at=task.source_modified_at,
            synced_at=now,
        )

    def _new_row(
        self,
        *,
        task: NormalizedSalesforceTask,
        owner_hit: ActivityDimensionHit | None,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
        related_local_id: UUID | None,
        related_name: str | None,
        now: datetime,
    ) -> CRMTask:
        return CRMTask(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            deal_id=deal_hit.internal_id if deal_hit is not None else None,
            title=_task_title(task),
            description=task.body,
            status=task.status or "NOT_STARTED",
            priority=task.priority or "NONE",
            due_date=task.due_date.date() if task.due_date is not None else None,
            due_at=task.due_date,
            is_all_day=False,
            completed_at=task.completed_date,
            assigned_to_user_id=owner_hit.internal_id if owner_hit is not None else None,
            assigned_to_name=owner_hit.name if owner_hit is not None else None,
            assigned_to_email=owner_hit.email if owner_hit is not None else None,
            owner_user_id=owner_hit.internal_id if owner_hit is not None else None,
            owner_email=owner_hit.email if owner_hit is not None else None,
            source_created_at=task.source_created_at,
            source_modified_at=task.source_modified_at,
            is_deleted=False,
            task_metadata=_task_metadata(
                task=task,
                contact_hit=contact_hit,
                company_hit=company_hit,
                related_local_id=related_local_id,
                related_name=related_name,
                last_seen_at=now,
            ),
        )

    def _row_values(
        self,
        *,
        task_id: UUID | None = None,
        task: NormalizedSalesforceTask,
        owner_hit: ActivityDimensionHit | None,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
        related_local_id: UUID | None,
        related_name: str | None,
        last_seen_at: datetime,
    ) -> dict:
        return {
            "organisation_id": self.organisation_id,
            "integration_id": self.integration_id,
            "task_id": task_id or uuid4(),
            "deal_id": deal_hit.internal_id if deal_hit is not None else None,
            "title": _task_title(task),
            "description": task.body,
            "status": task.status or "NOT_STARTED",
            "priority": task.priority or "NONE",
            "due_date": task.due_date.date() if task.due_date is not None else None,
            "due_at": task.due_date,
            "is_all_day": False,
            "completed_at": task.completed_date,
            "assigned_to_user_id": owner_hit.internal_id if owner_hit is not None else None,
            "assigned_to_name": owner_hit.name if owner_hit is not None else None,
            "assigned_to_email": owner_hit.email if owner_hit is not None else None,
            "owner_user_id": owner_hit.internal_id if owner_hit is not None else None,
            "owner_email": owner_hit.email if owner_hit is not None else None,
            "source_created_at": task.source_created_at,
            "source_modified_at": task.source_modified_at,
            "is_deleted": False,
            "task_metadata": _task_metadata(
                task=task,
                contact_hit=contact_hit,
                company_hit=company_hit,
                related_local_id=related_local_id,
                related_name=related_name,
                last_seen_at=last_seen_at,
            ),
        }

    def _apply_updates(
        self,
        row: CRMTask,
        *,
        task: NormalizedSalesforceTask,
        owner_hit: ActivityDimensionHit | None,
        deal_hit: ActivityDimensionHit | None,
        contact_hit: ActivityDimensionHit | None,
        company_hit: ActivityDimensionHit | None,
        related_local_id: UUID | None,
        related_name: str | None,
        now: datetime,
        last_seen_at: datetime | None = None,
    ) -> None:
        row.deal_id = deal_hit.internal_id if deal_hit is not None else None
        row.title = _task_title(task)
        row.description = task.body
        row.status = task.status or "NOT_STARTED"
        row.priority = task.priority or "NONE"
        row.due_date = task.due_date.date() if task.due_date is not None else None
        row.due_at = task.due_date
        row.is_all_day = False
        row.completed_at = task.completed_date
        row.assigned_to_user_id = owner_hit.internal_id if owner_hit is not None else None
        row.assigned_to_name = owner_hit.name if owner_hit is not None else None
        row.assigned_to_email = owner_hit.email if owner_hit is not None else None
        row.owner_user_id = owner_hit.internal_id if owner_hit is not None else None
        row.owner_email = owner_hit.email if owner_hit is not None else None
        row.source_created_at = task.source_created_at
        row.source_modified_at = task.source_modified_at
        row.is_deleted = False
        row.task_metadata = _task_metadata(
            task=task,
            contact_hit=contact_hit,
            company_hit=company_hit,
            related_local_id=related_local_id,
            related_name=related_name,
            last_seen_at=last_seen_at or now,
        )


def _task_title(task: NormalizedSalesforceTask) -> str:
    subject = task.subject.strip() if task.subject else ""
    if subject:
        return subject
    body = " ".join(task.body.split()) if task.body else ""
    return body[:80].rstrip() if body else "Untitled task"


def _task_metadata(
    *,
    task: NormalizedSalesforceTask,
    contact_hit: ActivityDimensionHit | None,
    company_hit: ActivityDimensionHit | None,
    related_local_id: UUID | None,
    related_name: str | None,
    last_seen_at: datetime,
) -> dict:
    metadata = dict(task.properties)
    metadata.update(
        {
            "source": "CRM_SYNC",
            "provider": "salesforce",
            "task_type": "TASK",
            "activity_subtype": task.activity_subtype,
            "related_object_type": task.related_object_type,
            "related_external_id": task.related_external_id,
            "related_local_id": str(related_local_id) if related_local_id else None,
            "related_name": related_name,
            "contact_id": str(contact_hit.internal_id) if contact_hit is not None else None,
            "company_id": str(company_hit.internal_id) if company_hit is not None else None,
            "last_seen_at": last_seen_at.isoformat(),
        }
    )
    return metadata
