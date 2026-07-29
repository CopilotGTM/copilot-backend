"""Persistence for CRM collection sorting stored in the shared user document."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.crm_list_preference_models import (
    CRMListEntity,
    CRMListSortingPreferences,
    UpdateCRMListSortingRequest,
)
from src.domain.models.master_tables.user_models import User
from src.service.users.user_preferences import (
    CRM_LIST_SORT_FIELDS,
    merge_crm_list_sorting,
)


class CRMListPreferencesService:
    """Persist one authenticated user's sorting without replacing other settings."""

    def __init__(
        self,
        master_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
    ) -> None:
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def update_sorting(
        self,
        entity: CRMListEntity,
        request: UpdateCRMListSortingRequest,
    ) -> CRMListSortingPreferences:
        """Validate and save the selected sorting for one CRM collection."""
        if request.sort_by not in CRM_LIST_SORT_FIELDS[entity]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported {entity} sort field",
            )

        user = await self._get_current_user_row()
        user.preferences = merge_crm_list_sorting(
            user.preferences,
            entity,
            request.sort_by,
            request.sort_order,
        )
        await self.master_session.commit()
        await self.master_session.refresh(user)
        return CRMListSortingPreferences.model_validate(request.model_dump())

    async def _get_current_user_row(self) -> User:
        """Load the user through both identity and organisation trust boundaries."""
        result = await self.master_session.execute(
            select(User).where(
                col(User.user_id) == self.user_id,
                col(User.organisation_id) == self.organisation_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return user
