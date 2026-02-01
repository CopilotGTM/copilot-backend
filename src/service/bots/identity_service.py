"""External Identity Service.

Generic service for managing the mapping between external provider identities
(Teams, Slack, Zoom, etc.) and CopilotGTM users.

This service handles:
- Looking up CopilotGTM users by external identity
- Creating new identity mappings
- Updating conversation context for proactive messaging
- Deactivating mappings on logout
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables import User
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityMapping,
    ExternalIdentityProvider,
    ExternalIdentityProviderType,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class ExternalIdentityService:
    """Generic service for resolving external identities to CopilotGTM users.

    This service works with any supported provider (Teams, Slack, Zoom, etc.)
    by accepting the provider as a parameter.

    Methods:
    1. Look up a user by their external identity
    2. Create new identity mappings after successful auth
    3. Update conversation context for proactive messaging
    4. Deactivate mappings on logout
    """

    async def get_user_for_external_identity(
        self,
        session: AsyncSession,
        provider: ExternalIdentityProvider,
        external_user_id: str,
        external_tenant_id: str | None = None,
    ) -> tuple[User | None, ExternalIdentityMapping | None]:
        """Look up a CopilotGTM user by their external identity.

        Args:
            session: Database session
            provider: External provider (teams, slack, etc.)
            external_user_id: User ID from the external provider
            external_tenant_id: Tenant/workspace ID (optional, for Teams/Slack)

        Returns:
            Tuple of (User, ExternalIdentityMapping) if found and active,
            (None, None) otherwise
        """
        # Build the query to find active mapping
        conditions: list[Any] = [
            ExternalIdentityMapping.provider == provider,
            ExternalIdentityMapping.external_user_id == external_user_id,
            ExternalIdentityMapping.is_active == True,  # noqa: E712 (SQLAlchemy comparison)
        ]

        # Include tenant ID in lookup if provided
        if external_tenant_id:
            conditions.append(ExternalIdentityMapping.external_tenant_id == external_tenant_id)

        stmt = select(ExternalIdentityMapping).where(and_(*conditions))
        result = await session.execute(stmt)
        mapping = result.scalar_one_or_none()

        if not mapping:
            logger.debug(
                "No active %s identity mapping found for user %s",
                provider.value,
                external_user_id[:20],
            )
            return None, None

        # Fetch the associated user
        user_stmt = select(User).where(col(User.user_id) == mapping.user_id)
        user_result = await session.execute(user_stmt)
        user = user_result.scalar_one_or_none()

        if not user:
            logger.warning(
                "%s mapping exists but user not found: user_id=%s",
                provider.value,
                mapping.user_id,
            )
            return None, mapping

        logger.debug(
            "Found %s identity mapping: external_user=%s -> copilot_user=%s",
            provider.value,
            external_user_id[:20],
            user.email,
        )

        return user, mapping

    async def create_or_reactivate_identity_mapping(
        self,
        session: AsyncSession,
        user_id: UUID,
        organisation_id: UUID,
        provider: ExternalIdentityProvider,
        provider_type: ExternalIdentityProviderType,
        external_user_id: str,
        external_tenant_id: str | None = None,
        external_user_email: str | None = None,
        external_user_name: str | None = None,
        conversation_context: dict[str, Any] | None = None,
    ) -> tuple[ExternalIdentityMapping, bool]:
        """Create a new identity mapping, or reactivate an existing one.

        Handles the re-login-after-logout case: if a mapping already exists
        (active or inactive), it is reactivated and updated rather than
        attempting a duplicate INSERT.

        Args:
            session: Database session
            user_id: CopilotGTM user ID
            organisation_id: Organisation ID
            provider: External provider (teams, slack, etc.)
            provider_type: Type of provider (chat_bot, sso, oauth)
            external_user_id: User ID from external provider
            external_tenant_id: Tenant/workspace ID (optional)
            external_user_email: Email from provider (optional)
            external_user_name: Display name from provider (optional)
            conversation_context: Bot context for proactive messaging (optional)

        Returns:
            Tuple of (ExternalIdentityMapping, created) where created is True
            if a new mapping was created, False if an existing one was reactivated.
        """
        now = datetime.now(UTC)

        # Check for existing mapping (active or inactive)
        conditions: list[Any] = [
            ExternalIdentityMapping.provider == provider,
            ExternalIdentityMapping.external_user_id == external_user_id,
        ]
        if external_tenant_id:
            conditions.append(ExternalIdentityMapping.external_tenant_id == external_tenant_id)

        stmt = select(ExternalIdentityMapping).where(and_(*conditions))
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Reactivate and update existing mapping
            existing.is_active = True
            existing.user_id = user_id
            existing.organisation_id = organisation_id
            existing.provider_type = provider_type
            existing.external_user_email = external_user_email
            existing.external_user_name = external_user_name
            existing.conversation_context = conversation_context
            existing.last_used_at = now
            if not existing.linked_at:
                existing.linked_at = now

            session.add(existing)
            await session.commit()
            await session.refresh(existing)

            logger.info(
                "Reactivated %s identity mapping: external_user=%s -> user_id=%s",
                provider.value,
                external_user_id[:20],
                user_id,
            )
            return existing, False

        # Create new mapping
        mapping = ExternalIdentityMapping(
            user_id=user_id,
            organisation_id=organisation_id,
            provider=provider,
            provider_type=provider_type,
            external_user_id=external_user_id,
            external_tenant_id=external_tenant_id,
            external_user_email=external_user_email,
            external_user_name=external_user_name,
            conversation_context=conversation_context,
            is_active=True,
            linked_at=now,
            last_used_at=now,
        )

        session.add(mapping)
        await session.commit()
        await session.refresh(mapping)

        logger.info(
            "Created %s identity mapping: external_user=%s -> user_id=%s",
            provider.value,
            external_user_id[:20],
            user_id,
        )

        return mapping, True

    async def deactivate_mapping(
        self,
        session: AsyncSession,
        provider: ExternalIdentityProvider,
        external_user_id: str,
        external_tenant_id: str | None = None,
    ) -> bool:
        """Deactivate an external identity mapping (logout).

        Args:
            session: Database session
            provider: External provider
            external_user_id: User ID from external provider
            external_tenant_id: Tenant/workspace ID

        Returns:
            True if a mapping was deactivated, False if none found
        """
        conditions: list[Any] = [
            ExternalIdentityMapping.provider == provider,
            ExternalIdentityMapping.external_user_id == external_user_id,
            ExternalIdentityMapping.is_active == True,  # noqa: E712
        ]

        if external_tenant_id:
            conditions.append(ExternalIdentityMapping.external_tenant_id == external_tenant_id)

        stmt = select(ExternalIdentityMapping).where(and_(*conditions))
        result = await session.execute(stmt)
        mapping = result.scalar_one_or_none()

        if not mapping:
            logger.debug(
                "No active %s mapping to deactivate for user %s",
                provider.value,
                external_user_id[:20],
            )
            return False

        mapping.is_active = False
        session.add(mapping)
        await session.commit()

        logger.info(
            "Deactivated %s identity mapping for user %s",
            provider.value,
            external_user_id[:20],
        )

        return True

    async def get_mapping_status(
        self,
        session: AsyncSession,
        provider: ExternalIdentityProvider,
        external_user_id: str,
        external_tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Get the status of an external identity mapping.

        Args:
            session: Database session
            provider: External provider
            external_user_id: User ID from external provider
            external_tenant_id: Tenant/workspace ID

        Returns:
            Status dict with is_linked, user_email, linked_at
        """
        user, mapping = await self.get_user_for_external_identity(
            session, provider, external_user_id, external_tenant_id
        )

        if user and mapping:
            return {
                "is_linked": True,
                "user_email": user.email,
                "linked_at": mapping.linked_at.isoformat() if mapping.linked_at else None,
                "provider": provider.value,
            }

        return {
            "is_linked": False,
            "user_email": None,
            "linked_at": None,
            "provider": provider.value,
        }
