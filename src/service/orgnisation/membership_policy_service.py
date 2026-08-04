"""Read and update organisation-controlled domain membership policy."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.membership_models import (
    MembershipPolicyResponse,
    UpdateMembershipPolicyRequest,
)
from src.domain.models.master_tables import Organisation
from src.service.orgnisation.organisation_preferences import (
    merge_organisation_app_settings,
    normalize_organisation_preferences,
)


class MembershipPolicyService:
    def __init__(self, master_session: AsyncSession, organisation_id: UUID) -> None:
        self._session = master_session
        self._organisation_id = organisation_id

    async def get_policy(self) -> MembershipPolicyResponse:
        organisation = await self._organisation()
        membership = normalize_organisation_preferences(organisation.preferences)["membership"]
        return MembershipPolicyResponse.model_validate(membership)

    async def update_policy(
        self,
        request: UpdateMembershipPolicyRequest,
    ) -> MembershipPolicyResponse:
        organisation = await self._organisation()
        organisation.preferences = merge_organisation_app_settings(
            organisation.preferences,
            membership_domain_join_policy=request.domain_join_policy,
        )
        await self._session.commit()
        await self._session.refresh(organisation)
        return MembershipPolicyResponse.model_validate(organisation.preferences["membership"])

    async def _organisation(self) -> Organisation:
        organisation = await self._session.get(Organisation, self._organisation_id)
        if organisation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found"
            )
        return organisation
