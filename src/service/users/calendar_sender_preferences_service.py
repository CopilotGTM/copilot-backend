"""Read and update the preferred sender used by calendar invite workflows.

The preference lives in the existing master User.preferences JSON document.
Connected-account validation stays in the organization schema so a stale or
foreign email can never become an outbound sender default.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.calendar_sender_preference_models import CalendarSenderPreference
from src.domain.models.master_tables.user_models import User
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_master_async_session
from src.service.users.user_preferences import (
    merge_preferred_calendar_sender,
    preferred_calendar_sender,
)


class CalendarSenderPreferencesService:
    """Manage one authenticated user's preferred connected calendar."""

    _CALENDAR_TYPES = (IntegrationType.MICROSOFT, IntegrationType.GOOGLE)

    def __init__(
        self,
        *,
        master_session: AsyncSession,
        org_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
    ) -> None:
        """Bind preference writes to both the master user and tenant schemas."""
        self.master_session = master_session
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def update(self, sender_email: str | None) -> CalendarSenderPreference:
        """Set a connected calendar as preferred, or clear the current preference."""
        normalized = (sender_email or "").strip().lower() or None
        if normalized:
            # The organization and creator predicates are the trust boundary;
            # an email address alone must never select another user's token.
            integration_stmt = select(col(KnowledgeIntegration.integration_id)).where(
                col(KnowledgeIntegration.organisation_id) == self.organisation_id,
                col(KnowledgeIntegration.created_by_user_id) == self.user_id,
                col(KnowledgeIntegration.integration_type).in_(self._CALENDAR_TYPES),
                col(KnowledgeIntegration.is_active) == True,  # noqa: E712
                func.lower(col(KnowledgeIntegration.integration_email)) == normalized,
            )
            if (await self.org_session.execute(integration_stmt)).scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Preferred sender must be one of your connected calendars",
                )

        user = await self._current_user()
        user.preferences = merge_preferred_calendar_sender(user.preferences, normalized)
        await self.master_session.commit()
        await self.master_session.refresh(user)
        return CalendarSenderPreference(preferred_sender_email=normalized)

    async def _current_user(self) -> User:
        """Load the authenticated user inside the organization boundary."""
        stmt = select(User).where(
            col(User.user_id) == self.user_id,
            col(User.organisation_id) == self.organisation_id,
        )
        user = (await self.master_session.execute(stmt)).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return user


async def load_preferred_calendar_sender(
    *,
    organisation_id: UUID,
    user_id: UUID,
) -> str | None:
    """Read the preferred sender for an agent tool without holding an org session."""
    # Preference lookup is a short master-schema read performed before provider
    # work; keeping it separate avoids mixing master and tenant transactions.
    async for master_session in acquire_master_async_session():
        stmt = select(col(User.preferences)).where(
            col(User.user_id) == user_id,
            col(User.organisation_id) == organisation_id,
        )
        raw_preferences = (await master_session.execute(stmt)).scalar_one_or_none()
        return preferred_calendar_sender(raw_preferences)
    return None
