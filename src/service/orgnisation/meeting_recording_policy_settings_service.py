from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.meeting_recording_policy_models import (
    MeetingRecordingPolicyResponse,
    UpdateMeetingRecordingPolicyRequest,
)
from src.domain.models.master_tables import Organisation
from src.service.orgnisation.organisation_preferences import (
    merge_organisation_app_settings,
    organisation_preferences_for_settings,
)


class MeetingRecordingPolicySettingsService:
    def __init__(self, *, master_session: AsyncSession, organisation_id: UUID) -> None:
        self.master_session = master_session
        self.organisation_id = organisation_id

    async def get_policy(self) -> MeetingRecordingPolicyResponse:
        organisation = await self._get_organisation()
        return self._response(organisation.preferences)

    async def update_policy(
        self,
        request: UpdateMeetingRecordingPolicyRequest,
    ) -> MeetingRecordingPolicyResponse:
        organisation = await self._get_organisation()
        organisation.preferences = merge_organisation_app_settings(
            organisation.preferences,
            meeting_recording_bot_join_policy=request.bot_join_policy,
        )
        await self.master_session.commit()
        await self.master_session.refresh(organisation)
        return self._response(organisation.preferences)

    async def _get_organisation(self) -> Organisation:
        organisation = await self.master_session.get(Organisation, self.organisation_id)
        if organisation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organisation not found",
            )
        return organisation

    @staticmethod
    def _response(preferences: dict | None) -> MeetingRecordingPolicyResponse:
        meeting_recording = organisation_preferences_for_settings(preferences)["meeting_recording"]
        return MeetingRecordingPolicyResponse.model_validate(meeting_recording)
