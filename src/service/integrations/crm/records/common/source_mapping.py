from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import (
    CRMSourceKind,
    CRMSourceObjectMapping,
    CRMSourceObjectType,
)
from src.service.integrations.crm.records.common.query_chunks import (
    EXTERNAL_ID_QUERY_CHUNK_SIZE,
    iter_chunks,
)

_BULK_UPSERT_PARAMETER_BUDGET = 24_000


@dataclass(slots=True, frozen=True)
class IntegrationMappingUpsert:
    external_object_type: CRMSourceObjectType
    external_id: str
    local_object_type: CRMSourceObjectType
    local_object_id: UUID
    data_payload: dict[str, Any] | None
    source_created_at: datetime | None
    source_modified_at: datetime | None
    synced_at: datetime
    mapping_metadata: dict[str, Any] | None = None


class CRMSourceMappingHelper:
    """Integration-bound source identity operations for CRM record writers."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
        provider: str,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id
        self.provider = _normalize_provider(provider)

    async def load_local_id_map(
        self,
        *,
        object_type: CRMSourceObjectType,
        external_ids: Iterable[str] | None = None,
        include_deleted: bool = False,
    ) -> dict[str, UUID]:
        external_id_set = _compact_external_ids(external_ids)
        if external_ids is not None and not external_id_set:
            return {}

        rows = []
        external_id_batches = (
            [None]
            if external_id_set is None
            else list(iter_chunks(external_id_set, EXTERNAL_ID_QUERY_CHUNK_SIZE))
        )
        for external_id_batch in external_id_batches:
            stmt = select(
                col(CRMSourceObjectMapping.external_id),
                col(CRMSourceObjectMapping.local_object_id),
            ).where(
                col(CRMSourceObjectMapping.organisation_id) == self.organisation_id,
                col(CRMSourceObjectMapping.integration_id) == self.integration_id,
                col(CRMSourceObjectMapping.source_kind) == CRMSourceKind.INTEGRATION,
                col(CRMSourceObjectMapping.provider) == self.provider,
                col(CRMSourceObjectMapping.external_object_type) == object_type,
            )
            if not include_deleted:
                stmt = stmt.where(col(CRMSourceObjectMapping.is_deleted) == False)  # noqa: E712
            if external_id_batch is not None:
                stmt = stmt.where(col(CRMSourceObjectMapping.external_id).in_(external_id_batch))
            rows.extend((await self.session.execute(stmt)).all())
        return {
            external_id: local_object_id
            for external_id, local_object_id in rows
            if external_id is not None and local_object_id is not None
        }

    async def upsert_integration_mapping(
        self,
        *,
        external_object_type: CRMSourceObjectType,
        external_id: str,
        local_object_type: CRMSourceObjectType,
        local_object_id: UUID,
        data_payload: dict[str, Any] | None,
        source_created_at: datetime | None,
        source_modified_at: datetime | None,
        synced_at: datetime,
        mapping_metadata: dict[str, Any] | None = None,
    ) -> CRMSourceObjectMapping:
        normalized_external_id = _normalize_external_id(external_id)
        stmt = select(CRMSourceObjectMapping).where(
            col(CRMSourceObjectMapping.organisation_id) == self.organisation_id,
            col(CRMSourceObjectMapping.integration_id) == self.integration_id,
            col(CRMSourceObjectMapping.source_kind) == CRMSourceKind.INTEGRATION,
            col(CRMSourceObjectMapping.provider) == self.provider,
            col(CRMSourceObjectMapping.external_object_type) == external_object_type,
            col(CRMSourceObjectMapping.external_id) == normalized_external_id,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            insert_stmt = pg_insert(CRMSourceObjectMapping).values(
                organisation_id=self.organisation_id,
                source_kind=CRMSourceKind.INTEGRATION.value,
                integration_id=self.integration_id,
                provider=self.provider,
                external_object_type=external_object_type.value,
                external_id=normalized_external_id,
                local_object_type=local_object_type.value,
                local_object_id=local_object_id,
                data_payload=data_payload,
                mapping_metadata=mapping_metadata or {},
                source_created_at=source_created_at,
                source_modified_at=source_modified_at,
                synced_at=synced_at,
                last_seen_at=synced_at,
                is_deleted=False,
            )
            upsert_stmt = insert_stmt.on_conflict_do_update(
                index_elements=[
                    col(CRMSourceObjectMapping.organisation_id),
                    col(CRMSourceObjectMapping.integration_id),
                    col(CRMSourceObjectMapping.provider),
                    col(CRMSourceObjectMapping.external_object_type),
                    col(CRMSourceObjectMapping.external_id),
                ],
                index_where=text("source_kind = 'integration'"),
                set_={
                    "local_object_type": local_object_type.value,
                    "local_object_id": local_object_id,
                    "data_payload": data_payload,
                    **(
                        {"mapping_metadata": mapping_metadata}
                        if mapping_metadata is not None
                        else {}
                    ),
                    "source_created_at": source_created_at,
                    "source_modified_at": source_modified_at,
                    "synced_at": synced_at,
                    "last_seen_at": synced_at,
                    "is_deleted": False,
                },
            ).returning(CRMSourceObjectMapping)
            return (await self.session.execute(upsert_stmt)).scalar_one()

        existing.local_object_type = local_object_type
        existing.local_object_id = local_object_id
        existing.data_payload = data_payload
        if mapping_metadata is not None:
            existing.mapping_metadata = mapping_metadata
        existing.source_created_at = source_created_at
        existing.source_modified_at = source_modified_at
        existing.synced_at = synced_at
        existing.last_seen_at = synced_at
        existing.is_deleted = False
        return existing

    async def bulk_upsert_integration_mappings(
        self,
        rows: Iterable[IntegrationMappingUpsert],
    ) -> int:
        deduped: dict[tuple[CRMSourceObjectType, str], IntegrationMappingUpsert] = {}
        for row in rows:
            normalized_external_id = _normalize_external_id(row.external_id)
            deduped[(row.external_object_type, normalized_external_id)] = IntegrationMappingUpsert(
                external_object_type=row.external_object_type,
                external_id=normalized_external_id,
                local_object_type=row.local_object_type,
                local_object_id=row.local_object_id,
                data_payload=row.data_payload,
                source_created_at=row.source_created_at,
                source_modified_at=row.source_modified_at,
                synced_at=row.synced_at,
                mapping_metadata=row.mapping_metadata,
            )
        if not deduped:
            return 0

        with_metadata = [row for row in deduped.values() if row.mapping_metadata is not None]
        without_metadata = [row for row in deduped.values() if row.mapping_metadata is None]
        await self._bulk_upsert_integration_mapping_rows(with_metadata, update_metadata=True)
        await self._bulk_upsert_integration_mapping_rows(without_metadata, update_metadata=False)
        return len(deduped)

    async def _bulk_upsert_integration_mapping_rows(
        self,
        rows: list[IntegrationMappingUpsert],
        *,
        update_metadata: bool,
    ) -> None:
        if not rows:
            return
        values = [self._bulk_mapping_values(row) for row in rows]
        max_rows_per_statement = max(1, _BULK_UPSERT_PARAMETER_BUDGET // len(values[0]))
        for offset in range(0, len(values), max_rows_per_statement):
            await self._bulk_upsert_integration_mapping_chunk(
                values[offset : offset + max_rows_per_statement],
                update_metadata=update_metadata,
            )

    def _bulk_mapping_values(self, row: IntegrationMappingUpsert) -> dict[str, Any]:
        return {
            "mapping_id": uuid4(),
            "organisation_id": self.organisation_id,
            "source_kind": CRMSourceKind.INTEGRATION.value,
            "integration_id": self.integration_id,
            "source_run_id": None,
            "provider": self.provider,
            "external_object_type": row.external_object_type.value,
            "external_id": row.external_id,
            "local_object_type": row.local_object_type.value,
            "local_object_id": row.local_object_id,
            "data_payload": row.data_payload,
            "mapping_metadata": row.mapping_metadata or {},
            "source_created_at": row.source_created_at,
            "source_modified_at": row.source_modified_at,
            "synced_at": row.synced_at,
            "last_seen_at": row.synced_at,
            "is_deleted": False,
        }

    async def _bulk_upsert_integration_mapping_chunk(
        self,
        rows: list[dict[str, Any]],
        *,
        update_metadata: bool,
    ) -> None:
        insert_stmt = pg_insert(CRMSourceObjectMapping).values(rows)
        excluded = insert_stmt.excluded
        update_values = {
            "local_object_type": excluded.local_object_type,
            "local_object_id": excluded.local_object_id,
            "data_payload": excluded.data_payload,
            "source_created_at": excluded.source_created_at,
            "source_modified_at": excluded.source_modified_at,
            "synced_at": excluded.synced_at,
            "last_seen_at": excluded.last_seen_at,
            "is_deleted": False,
            "updated_at": func.now(),
        }
        if update_metadata:
            update_values["mapping_metadata"] = excluded.mapping_metadata
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=[
                col(CRMSourceObjectMapping.organisation_id),
                col(CRMSourceObjectMapping.integration_id),
                col(CRMSourceObjectMapping.provider),
                col(CRMSourceObjectMapping.external_object_type),
                col(CRMSourceObjectMapping.external_id),
            ],
            index_where=text("source_kind = 'integration'"),
            set_=update_values,
        )
        await self.session.execute(stmt)

    async def mark_missing_deleted(
        self,
        *,
        object_type: CRMSourceObjectType,
        seen_external_ids: Iterable[str],
    ) -> int:
        seen = _compact_external_ids(seen_external_ids) or set()
        stmt = select(CRMSourceObjectMapping).where(
            col(CRMSourceObjectMapping.organisation_id) == self.organisation_id,
            col(CRMSourceObjectMapping.integration_id) == self.integration_id,
            col(CRMSourceObjectMapping.source_kind) == CRMSourceKind.INTEGRATION,
            col(CRMSourceObjectMapping.provider) == self.provider,
            col(CRMSourceObjectMapping.external_object_type) == object_type,
            col(CRMSourceObjectMapping.is_deleted) == False,  # noqa: E712
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        deleted = 0
        for mapping in rows:
            if mapping.external_id in seen:
                continue
            mapping.is_deleted = True
            deleted += 1
        return deleted

    async def mark_external_ids_deleted(
        self,
        *,
        object_type: CRMSourceObjectType,
        external_ids: Iterable[str],
    ) -> dict[str, UUID]:
        external_id_set = _compact_external_ids(external_ids) or set()
        if not external_id_set:
            return {}

        rows = []
        for external_id_batch in iter_chunks(external_id_set, EXTERNAL_ID_QUERY_CHUNK_SIZE):
            stmt = select(CRMSourceObjectMapping).where(
                col(CRMSourceObjectMapping.organisation_id) == self.organisation_id,
                col(CRMSourceObjectMapping.integration_id) == self.integration_id,
                col(CRMSourceObjectMapping.source_kind) == CRMSourceKind.INTEGRATION,
                col(CRMSourceObjectMapping.provider) == self.provider,
                col(CRMSourceObjectMapping.external_object_type) == object_type,
                col(CRMSourceObjectMapping.external_id).in_(external_id_batch),
                col(CRMSourceObjectMapping.is_deleted) == False,  # noqa: E712
            )
            rows.extend((await self.session.execute(stmt)).scalars().all())
        deleted: dict[str, UUID] = {}
        for mapping in rows:
            mapping.is_deleted = True
            deleted[mapping.external_id] = mapping.local_object_id
        return deleted


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if not normalized:
        raise ValueError("CRM source mapping provider cannot be empty")
    return normalized


def _normalize_external_id(external_id: str) -> str:
    normalized = external_id.strip()
    if not normalized:
        raise ValueError("CRM source mapping external_id cannot be empty")
    return normalized


def _compact_external_ids(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {_normalize_external_id(value) for value in values if isinstance(value, str)}
