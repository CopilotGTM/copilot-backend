from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.onboarding_preference_models import (
    OnboardingPreferences,
    OnboardingStepId,
)
from src.domain.models.master_tables.user_models import User
from src.service.orgnisation.organisation_policy_service import OrganisationPolicyService
from src.service.users.user_preferences import (
    merge_skipped_onboarding_step,
    normalize_onboarding_preferences,
)


class OnboardingPreferencesService:
    def __init__(
        self,
        master_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
    ) -> None:
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def skip_step(self, step_id: OnboardingStepId) -> OnboardingPreferences:
        policy = await OrganisationPolicyService(self.master_session).get_policy_snapshot(
            self.organisation_id
        )
        step = next(
            (
                item
                for item in policy.get("onboarding", {}).get("steps", [])
                if item.get("id") == step_id
            ),
            None,
        )
        if step is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Onboarding step is not configured",
            )
        if not step["skippable"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This onboarding step is required",
            )

        user = await self._get_current_user_row()
        user.preferences = merge_skipped_onboarding_step(user.preferences, step_id)
        await self.master_session.commit()
        await self.master_session.refresh(user)
        return OnboardingPreferences.model_validate(
            normalize_onboarding_preferences(user.preferences.get("onboarding"))
        )

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
