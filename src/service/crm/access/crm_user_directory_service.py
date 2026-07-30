"""Organisation-scoped lookup for canonical CRM users."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.users import CRMUser

CRMUserResolutionStatus = Literal["resolved", "ambiguous", "not_found"]
MAX_CRM_USER_QUERY_CHARS = 200


@dataclass(frozen=True)
class CRMUserDirectoryResult:
    """Bounded CRM-user matches with an explicit resolution status."""

    status: CRMUserResolutionStatus
    candidates: list[CRMUser]


class CRMUserDirectoryService:
    """Resolve team members from the existing canonical CRM user directory."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def search(self, query: str, *, limit: int = 10) -> CRMUserDirectoryResult:
        """Resolve exact identities first, then return bounded partial matches."""
        normalized = " ".join(query.strip().lower().split())
        if not normalized or len(normalized) > MAX_CRM_USER_QUERY_CHARS:
            return CRMUserDirectoryResult(status="not_found", candidates=[])

        base_filters = (
            col(CRMUser.organisation_id) == self.organisation_id,
            col(CRMUser.is_deleted).is_not(True),
            col(CRMUser.is_active).is_(True),
        )
        full_name = func.lower(
            func.trim(
                func.coalesce(
                    col(CRMUser.full_name),
                    func.concat(
                        func.coalesce(col(CRMUser.first_name), ""),
                        " ",
                        func.coalesce(col(CRMUser.last_name), ""),
                    ),
                )
            )
        )
        normalized_email = func.lower(func.trim(col(CRMUser.email)))

        exact_conditions = [
            normalized_email == normalized,
            full_name == normalized,
        ]
        with contextlib.suppress(ValueError):
            exact_conditions.insert(0, col(CRMUser.user_id) == UUID(normalized))

        exact_stmt = (
            select(CRMUser)
            .where(*base_filters, or_(*exact_conditions))
            .order_by(col(CRMUser.full_name).asc().nulls_last(), col(CRMUser.user_id).asc())
            .limit(max(2, min(limit, 10)))
        )
        exact = list((await self.session.execute(exact_stmt)).scalars().all())
        if exact:
            return CRMUserDirectoryResult(
                status="resolved" if len(exact) == 1 else "ambiguous",
                candidates=exact,
            )

        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        partial_stmt = (
            select(CRMUser)
            .where(
                *base_filters,
                or_(
                    col(CRMUser.email).ilike(like, escape="\\"),
                    col(CRMUser.full_name).ilike(like, escape="\\"),
                    col(CRMUser.first_name).ilike(like, escape="\\"),
                    col(CRMUser.last_name).ilike(like, escape="\\"),
                ),
            )
            .order_by(col(CRMUser.full_name).asc().nulls_last(), col(CRMUser.user_id).asc())
            .limit(max(1, min(limit, 10)))
        )
        partial = list((await self.session.execute(partial_stmt)).scalars().all())
        if not partial:
            return CRMUserDirectoryResult(status="not_found", candidates=[])
        return CRMUserDirectoryResult(
            status="resolved" if len(partial) == 1 else "ambiguous",
            candidates=partial,
        )


__all__ = [
    "CRMUserDirectoryResult",
    "CRMUserDirectoryService",
    "CRMUserResolutionStatus",
    "MAX_CRM_USER_QUERY_CHARS",
]
