"""Create tenant CRM defaults and the CRM identity for one application user."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.users import CRMUser
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService


@dataclass(frozen=True)
class TenantUserSnapshot:
    """Master-user fields safe to carry across the tenant database boundary."""

    user_id: UUID
    organisation_id: UUID
    email: str
    first_name: str
    last_name: str
    user_role: str
    job_title: str | None
    is_active: bool


@dataclass(frozen=True)
class UserTenantSetupResult:
    """Report whether this call created the tenant CRM identity."""

    crm_user: CRMUser | None
    created: bool


class UserTenantSetupService:
    """Idempotently provision CRM defaults and one app-user-backed CRM user."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def ensure_user(
        self,
        user: TenantUserSnapshot,
        *,
        create_crm_user: bool = True,
    ) -> UserTenantSetupResult:
        """Ensure tenant defaults and optionally the user's CRM identity exist."""
        setup = await InternalCRMSetupService(
            self._session,
            user.organisation_id,
            user.user_id,
        ).setup_internal_crm(commit=False)

        if not create_crm_user:
            await self._session.commit()
            return UserTenantSetupResult(crm_user=None, created=False)

        # Both IDs form the tenant trust boundary; a user UUID alone is not
        # sufficient when this service is reused by public signup workers.
        result = await self._session.execute(
            select(CRMUser).where(
                col(CRMUser.organisation_id) == user.organisation_id,
                col(CRMUser.user_id) == user.user_id,
            )
        )
        crm_user = result.scalars().first()
        created = crm_user is None
        if crm_user is None:
            crm_user = CRMUser(
                user_id=user.user_id,
                organisation_id=user.organisation_id,
                integration_id=setup.integration.integration_id,
            )

        crm_user.email = user.email
        crm_user.first_name = user.first_name
        crm_user.last_name = user.last_name
        crm_user.full_name = (
            " ".join(part for part in (user.first_name, user.last_name) if part) or None
        )
        crm_user.user_role = user.user_role
        crm_user.job_title = user.job_title
        crm_user.is_active = user.is_active
        crm_user.is_deleted = False
        self._session.add(crm_user)
        await self._session.flush()
        await self._session.commit()
        return UserTenantSetupResult(crm_user=crm_user, created=created)
