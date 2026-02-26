"""Bot Platform Installation Service.

Generic service for managing bot installations across platforms (Slack, Teams, Google Chat).
Handles CRUD operations on the bot_platform_installations table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from src.domain.models.master_tables.bot_platform_installation_models import (
    BotPlatformInstallation,
    BotPlatformProvider,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class BotInstallationService:
    """Service for managing bot platform installations."""

    async def upsert_installation(
        self,
        session: AsyncSession,
        provider: BotPlatformProvider,
        external_workspace_id: str,
        bot_token: str | None = None,
        bot_user_id: str | None = None,
        external_workspace_name: str | None = None,
        installed_by_external_user_id: str | None = None,
        organisation_id: UUID | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> BotPlatformInstallation:
        """Create or update a bot installation for a workspace.

        If an installation already exists for this provider + workspace,
        it is updated with the new token and reactivated.

        Returns the installation record.
        """
        stmt = select(BotPlatformInstallation).where(
            col(BotPlatformInstallation.provider) == provider.value,
            col(BotPlatformInstallation.external_workspace_id) == external_workspace_id,
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            existing.bot_token = bot_token
            existing.bot_user_id = bot_user_id
            existing.is_active = True
            existing.last_used_at = datetime.now(UTC)
            if external_workspace_name:
                existing.external_workspace_name = external_workspace_name
            if installed_by_external_user_id:
                existing.installed_by_external_user_id = installed_by_external_user_id
            if organisation_id:
                existing.organisation_id = organisation_id
            if extra_data:
                existing.extra_data = {**(existing.extra_data or {}), **extra_data}
            session.add(existing)
            await session.commit()
            await session.refresh(existing)
            logger.info(
                "Updated %s installation for workspace=%s",
                provider.value,
                external_workspace_id,
            )
            return existing

        installation = BotPlatformInstallation(
            provider=provider,
            external_workspace_id=external_workspace_id,
            external_workspace_name=external_workspace_name,
            bot_token=bot_token,
            bot_user_id=bot_user_id,
            extra_data=extra_data,
            organisation_id=organisation_id,
            installed_by_external_user_id=installed_by_external_user_id,
            is_active=True,
            installed_at=datetime.now(UTC),
        )
        session.add(installation)
        try:
            await session.commit()
            await session.refresh(installation)
            logger.info(
                "Created %s installation for workspace=%s",
                provider.value,
                external_workspace_id,
            )
        except IntegrityError:
            await session.rollback()
            # Race condition: re-fetch
            result = await session.execute(stmt)
            installation = result.scalar_one()
            installation.bot_token = bot_token
            installation.bot_user_id = bot_user_id
            installation.is_active = True
            session.add(installation)
            await session.commit()
            await session.refresh(installation)

        return installation

    async def get_installation(
        self,
        session: AsyncSession,
        provider: BotPlatformProvider,
        external_workspace_id: str,
    ) -> BotPlatformInstallation | None:
        """Look up an active installation by provider + workspace ID."""
        stmt = select(BotPlatformInstallation).where(
            col(BotPlatformInstallation.provider) == provider.value,
            col(BotPlatformInstallation.external_workspace_id) == external_workspace_id,
            col(BotPlatformInstallation.is_active).is_(True),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_bot_token(
        self,
        session: AsyncSession,
        provider: BotPlatformProvider,
        external_workspace_id: str,
    ) -> str | None:
        """Get the bot token for a workspace. Returns None if not found or inactive."""
        installation = await self.get_installation(session, provider, external_workspace_id)
        if installation:
            return installation.bot_token
        return None

    async def deactivate_installation(
        self,
        session: AsyncSession,
        provider: BotPlatformProvider,
        external_workspace_id: str,
    ) -> bool:
        """Mark an installation as inactive (uninstalled)."""
        stmt = (
            update(BotPlatformInstallation)
            .where(
                col(BotPlatformInstallation.provider) == provider.value,
                col(BotPlatformInstallation.external_workspace_id) == external_workspace_id,
            )
            .values(is_active=False)
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0

    async def touch_last_used(
        self,
        session: AsyncSession,
        provider: BotPlatformProvider,
        external_workspace_id: str,
    ) -> None:
        """Update last_used_at timestamp for an installation."""
        stmt = (
            update(BotPlatformInstallation)
            .where(
                col(BotPlatformInstallation.provider) == provider.value,
                col(BotPlatformInstallation.external_workspace_id) == external_workspace_id,
            )
            .values(last_used_at=datetime.now(UTC))
        )
        await session.execute(stmt)
        await session.commit()

    async def link_organisation(
        self,
        session: AsyncSession,
        provider: BotPlatformProvider,
        external_workspace_id: str,
        organisation_id: UUID,
    ) -> bool:
        """Link an installation to a CopilotGTM organisation."""
        stmt = (
            update(BotPlatformInstallation)
            .where(
                col(BotPlatformInstallation.provider) == provider.value,
                col(BotPlatformInstallation.external_workspace_id) == external_workspace_id,
            )
            .values(organisation_id=organisation_id)
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0
