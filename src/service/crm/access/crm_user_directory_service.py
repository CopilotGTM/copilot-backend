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
from src.service.crm.utils.identity import is_valid_email, normalize_spoken_email_query

CRMUserResolutionStatus = Literal["resolved", "ambiguous", "not_found"]
CRMUserMatchType = Literal["exact", "partial", "fuzzy_name", "fuzzy_email"]
MAX_CRM_USER_QUERY_CHARS = 200
# A first-name typo such as "Jonatan"/"Jonathan" clears this threshold while
# unrelated names do not. Multiple qualifying people still remain ambiguous.
CRM_USER_NAME_FUZZY_THRESHOLD = 0.40
CRM_USER_EMAIL_FUZZY_THRESHOLD = 0.55


@dataclass(frozen=True)
class CRMUserDirectoryResult:
    """Bounded CRM-user matches with an explicit resolution status."""

    status: CRMUserResolutionStatus
    candidates: list[CRMUser]
    match_type: CRMUserMatchType | None = None


class CRMUserDirectoryService:
    """Resolve team members from the existing canonical CRM user directory."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def search(self, query: str, *, limit: int = 10) -> CRMUserDirectoryResult:
        """Resolve one active team identity without guessing among similar people.

        Agent and calendar callers share this exact-first lookup. Fuzzy email
        matches are always returned as ambiguous suggestions because a close
        address is not safe enough to become a recipient or write target.
        """
        normalized = normalize_spoken_email_query(query)
        if not normalized or len(normalized) > MAX_CRM_USER_QUERY_CHARS:
            return CRMUserDirectoryResult(status="not_found", candidates=[])
        email_query = is_valid_email(normalized)

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
                match_type="exact",
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
            effective_limit = max(1, min(limit, 10))
            if email_query:
                query_local, query_domain = normalized.split("@", 1)
                stored_local = func.split_part(normalized_email, "@", 1)
                stored_domain = func.split_part(normalized_email, "@", 2)
                local_score = func.similarity(stored_local, query_local)
                domain_score = func.similarity(stored_domain, query_domain)
                email_score = local_score * 0.75 + domain_score * 0.25
                fuzzy_stmt = (
                    select(CRMUser)
                    .where(
                        *base_filters,
                        normalized_email.is_not(None),
                        local_score >= CRM_USER_NAME_FUZZY_THRESHOLD,
                        email_score >= CRM_USER_EMAIL_FUZZY_THRESHOLD,
                    )
                    .order_by(email_score.desc(), col(CRMUser.user_id).asc())
                    .limit(effective_limit)
                )
                fuzzy = list((await self.session.execute(fuzzy_stmt)).scalars().all())
                return CRMUserDirectoryResult(
                    # Even one close email is a suggestion, never an identity decision.
                    status="ambiguous" if fuzzy else "not_found",
                    candidates=fuzzy,
                    match_type="fuzzy_email" if fuzzy else None,
                )

            query_tokens = normalized.split()
            first_token = query_tokens[0]
            last_token = query_tokens[-1]
            stored_first_name = func.lower(func.coalesce(col(CRMUser.first_name), ""))
            stored_last_name = func.lower(func.coalesce(col(CRMUser.last_name), ""))
            first_name_score = func.similarity(stored_first_name, first_token)
            last_name_score = func.similarity(stored_last_name, last_token)
            name_score = func.greatest(
                func.similarity(full_name, normalized),
                func.word_similarity(full_name, normalized),
                first_name_score,
                last_name_score,
            )
            fuzzy_filter = name_score >= CRM_USER_NAME_FUZZY_THRESHOLD
            if len(query_tokens) > 1:
                # A complete stored surname must resemble the spoken surname.
                # Initial-only profiles cannot satisfy that check, so a unique
                # first-name recovery remains available for those incomplete rows.
                fuzzy_filter &= (func.length(stored_last_name) == 1) | (
                    last_name_score >= CRM_USER_NAME_FUZZY_THRESHOLD
                )
            fuzzy_stmt = (
                select(CRMUser)
                .where(*base_filters, fuzzy_filter)
                .order_by(name_score.desc(), col(CRMUser.full_name).asc().nulls_last())
                .limit(effective_limit)
            )
            fuzzy = list((await self.session.execute(fuzzy_stmt)).scalars().all())
            return CRMUserDirectoryResult(
                status="resolved" if len(fuzzy) == 1 else "ambiguous" if fuzzy else "not_found",
                candidates=fuzzy,
                match_type="fuzzy_name" if fuzzy else None,
            )
        return CRMUserDirectoryResult(
            status="resolved" if len(partial) == 1 else "ambiguous",
            candidates=partial,
            match_type="partial",
        )


__all__ = [
    "CRMUserDirectoryResult",
    "CRMUserDirectoryService",
    "CRMUserMatchType",
    "CRMUserResolutionStatus",
    "CRM_USER_EMAIL_FUZZY_THRESHOLD",
    "CRM_USER_NAME_FUZZY_THRESHOLD",
    "MAX_CRM_USER_QUERY_CHARS",
]
