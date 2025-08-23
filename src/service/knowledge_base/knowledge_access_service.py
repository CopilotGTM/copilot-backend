"""Service layer for listing and downloading knowledge sources.

Responsibilities:
    * Provide key‑set paginated listing of `OrganisationSource` records.
    * Optional filtering by status, source_type, filename search query.
    * Secure generation of short‑lived read SAS URLs for original file & thumbnails.

Design notes:
    * Pagination mirrors existing pattern (see `user_management.list_users` & chat listing):
        ORDER BY created_at DESC, source_id DESC with `limit + 1` probe row.
    * Cursor is base64( { ts, id } ) using shared helpers in `infra.utils.cursor`.
    * We only generate SAS URLs when explicitly requested (`include_urls=True`) to
      avoid N Azure calls per page list unnecessarily.
    * Session passed in is organisation‑scoped (search_path already applied).
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple
import asyncio
from uuid import UUID
from datetime import datetime

from sqlmodel import Session, select, or_, and_
from sqlalchemy import func

from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
    SourceType,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.cursor import decode_cursor
from src.infra.utils.logger import get_logger

logger = get_logger("knowledge_access_service")


class KnowledgeAccessServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class KnowledgeAccessService:
    def __init__(self, org_session: Session, organisation_id: UUID, user_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.storage = AzureOrganisationBlobStorage(organisation_id)

    @property
    def session(self) -> Session:  # pragma: no cover (delegation)
        return self.org_session

    async def close(self):  # pragma: no cover - simple cleanup
        try:
            await self.storage.close()
        except Exception:
            logger.debug("Storage close swallow (access service)", exc_info=True)

    # -------- Listing ----------
    def list_sources(
        self,
        limit: int = 20,
        cursor: Optional[str] = None,
        statuses: Optional[Iterable[OrganisationSourceStatus | str]] = None,
        source_types: Optional[Iterable[SourceType | str]] = None,
        q: Optional[str] = None,
    ) -> Tuple[List[OrganisationSource], Optional[str]]:
        """Return page of sources (key‑set pagination).

        Returns (items, next_cursor).
        """
        stmt = select(OrganisationSource).where(
            OrganisationSource.organisation_id == self.organisation_id,
            OrganisationSource.user_id == self.user_id,
        )

        if statuses:
            status_values = [s.value if hasattr(s, "value") else str(s) for s in statuses]
            stmt = stmt.where(OrganisationSource.status.in_(status_values))  # type: ignore[arg-type]

        if source_types:
            type_values = [s.value if hasattr(s, "value") else str(s) for s in source_types]
            stmt = stmt.where(OrganisationSource.source_type.in_(type_values))  # type: ignore[arg-type]

        if q:
            like_expr = f"%{q.lower()}%"
            stmt = stmt.where(OrganisationSource.filename.ilike(like_expr))  # type: ignore[attr-defined]

        if cursor:
            try:
                ts, sid = decode_cursor(cursor)
            except Exception:
                raise KnowledgeAccessServiceError("Invalid cursor", status_code=400)
            stmt = stmt.where(
                or_(
                    OrganisationSource.created_at < ts,
                    and_(
                        OrganisationSource.created_at == ts,
                        OrganisationSource.source_id < sid,
                    ),
                )
            )

        stmt = stmt.order_by(
            OrganisationSource.created_at.desc(),
            OrganisationSource.source_id.desc(),
        ).limit(limit + 1)

        rows: List[OrganisationSource] = self.org_session.exec(stmt).all()
        has_more = len(rows) == limit + 1
        items = rows[:limit]
        next_cursor: Optional[str] = None
        if has_more and items:
            # encode cursor lazily to avoid circular import (import inside)
            from src.infra.utils.cursor import encode_cursor

            last = items[-1]
            next_cursor = encode_cursor(last.created_at, last.source_id)
        return items, next_cursor

    # -------- Download ----------
    async def generate_download_url(
        self, source_id: UUID, minutes: int = 120, download_filename: Optional[str] = None
    ) -> str:
        src: OrganisationSource | None = self.org_session.get(OrganisationSource, source_id)
        if (
            not src
            or src.organisation_id != self.organisation_id
            or src.user_id != self.user_id
        ):
            raise KnowledgeAccessServiceError("Source not found or access denied", status_code=404)
        if not src.storage_path:
            raise KnowledgeAccessServiceError("Source has no storage path", status_code=400)
        try:
            return await self.storage.generate_read_sas(
                src.storage_path, minutes=minutes, download_filename=download_filename or src.filename
            )
        except Exception as e:  # pragma: no cover - network/azure path
            logger.error("Failed generating read SAS for %s: %s", source_id, e, exc_info=True)
            raise KnowledgeAccessServiceError("Could not generate download URL") from e

    async def build_item_dict(self, src: OrganisationSource, include_urls: bool) -> dict:
        status_val = src.status.value if hasattr(src.status, "value") else str(src.status)
        data = {
            "source_id": str(src.source_id),
            "filename": src.filename,
            "mimetype": src.mimetype,
            "size_bytes": src.size_bytes,
            "status": status_val,
            "created_at": src.created_at,
            "updated_at": src.updated_at,
            "error_message": src.error_message,
            "checksum": getattr(src, "checksum", None),
        }
        if include_urls:
            tasks: List[asyncio.Task] = []
            key_order: List[str] = []

            async def _gen(path: str, key: str):
                try:
                    return key, await self.storage.generate_read_sas(path, minutes=120)
                except Exception:  # pragma: no cover - network variability
                    logger.debug("Failed generating %s for %s", key, src.source_id, exc_info=True)
                    return None

            for attr, key in (
                ("thumbnail_small_path", "thumbnail_small_url"),
                ("thumbnail_medium_path", "thumbnail_medium_url"),
            ):
                path = getattr(src, attr, None)
                if path:
                    key_order.append(key)
                    tasks.append(asyncio.create_task(_gen(path, key)))

            if tasks:
                results = await asyncio.gather(*tasks)
                for res in results:
                    if res:
                        k, v = res
                        data[k] = v
        return data

    # -------- Stats ----------
    def get_stats(self) -> dict:
        """Return aggregated counts for dashboard usage.

        Counts are restricted to current user + organisation (matching list logic).
        """
        base_filters = [
            OrganisationSource.organisation_id == self.organisation_id,
            OrganisationSource.user_id == self.user_id,
        ]

        status_stmt = (
            select(OrganisationSource.status, func.count())
            .where(*base_filters)
            .group_by(OrganisationSource.status)
        )
        status_rows = self.org_session.exec(status_stmt).all()
        by_status: dict[str, int] = {}
        total = 0
        for status_val, count in status_rows:
            key = status_val.value if hasattr(status_val, "value") else str(status_val)
            by_status[key] = int(count)
            total += int(count)

        type_stmt = (
            select(OrganisationSource.source_type, func.count())
            .where(*base_filters)
            .group_by(OrganisationSource.source_type)
        )
        type_rows = self.org_session.exec(type_stmt).all()
        by_type: dict[str, int] = {}
        for stype, count in type_rows:
            key = stype.value if hasattr(stype, "value") else str(stype)
            by_type[key] = int(count)

        return {"total": total, "by_status": by_status, "by_type": by_type}
