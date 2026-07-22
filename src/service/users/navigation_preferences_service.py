from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.navigation_preference_models import (
    NavigationPreferences,
    UpdateNavigationPreferencesRequest,
)
from src.domain.models.master_tables.user_models import User
from src.service.users.user_preferences import merge_navigation_preferences


class NavigationPreferencesService:
    def __init__(
        self,
        master_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
    ) -> None:
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def update_preferences(
        self,
        request: UpdateNavigationPreferencesRequest,
    ) -> NavigationPreferences:
        user = await self._get_current_user_row()
        navigation = request.model_dump(mode="json")
        user.preferences = merge_navigation_preferences(user.preferences, navigation)
        await self.master_session.commit()
        await self.master_session.refresh(user)
        return NavigationPreferences.model_validate(navigation)

    async def _get_current_user_row(self) -> User:
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
