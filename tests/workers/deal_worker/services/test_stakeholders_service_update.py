"""Tests for StakeholdersService.update_stakeholder."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.api.schemas.crm_models import UpdateStakeholderRequest
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.insights.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
)


def _service() -> StakeholdersService:
    session = AsyncMock()
    return StakeholdersService(session, uuid4())


@pytest.mark.asyncio
async def test_update_stakeholder_applies_updates_and_tracks_manual_overrides() -> None:
    service = _service()
    participant = CRMDealParticipant(
        participant_id=uuid4(),
        deal_id=uuid4(),
        organisation_id=uuid4(),
        integration_id=uuid4(),
        participant_type=CRMParticipantType.CONTACT,
        related_email="person@example.com",
        related_name="Old Name",
        job_title="Old Title",
        stance="UNKNOWN",
        decision_power="UNKNOWN",
        temperature="UNKNOWN",
        participant_source="MANUAL",
        approval_status="APPROVED",
    )
    service.get_stakeholder = AsyncMock(return_value=participant)  # type: ignore[method-assign]

    request = UpdateStakeholderRequest(
        name="Updated Name",
        job_title="Updated Title",
        motivation="Updated motivation",
    )

    result = await service.update_stakeholder(uuid4(), participant.participant_id, request)

    assert result.name == "Updated Name"
    assert result.job_title == "Updated Title"
    assert result.motivation == "Updated motivation"
    assert participant.related_name == "Updated Name"
    assert participant.job_title == "Updated Title"
    assert participant.motivation == "Updated motivation"
    service.session.commit.assert_awaited_once()
    service.session.refresh.assert_awaited_once_with(participant)


@pytest.mark.asyncio
async def test_update_stakeholder_raises_404_when_missing() -> None:
    service = _service()
    service.get_stakeholder = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(StakeholdersServiceError) as exc:
        await service.update_stakeholder(
            uuid4(),
            uuid4(),
            UpdateStakeholderRequest(name="Updated"),
        )

    assert exc.value.status_code == 404
    assert exc.value.message == "Stakeholder not found"


@pytest.mark.asyncio
async def test_update_stakeholder_raises_400_for_empty_payload() -> None:
    service = _service()
    stakeholder = SimpleNamespace(stakeholder_data=None)
    service.get_stakeholder = AsyncMock(return_value=stakeholder)  # type: ignore[method-assign]

    with pytest.raises(StakeholdersServiceError) as exc:
        await service.update_stakeholder(uuid4(), uuid4(), UpdateStakeholderRequest())

    assert exc.value.status_code == 400
    assert exc.value.message == "No fields provided for update"


@pytest.mark.asyncio
async def test_update_stakeholder_raises_400_when_non_nullable_field_is_null() -> None:
    service = _service()
    stakeholder = SimpleNamespace(stakeholder_data=None)
    service.get_stakeholder = AsyncMock(return_value=stakeholder)  # type: ignore[method-assign]

    with pytest.raises(StakeholdersServiceError) as exc:
        await service.update_stakeholder(
            uuid4(),
            uuid4(),
            UpdateStakeholderRequest.model_validate({"stance": None}),
        )

    assert exc.value.status_code == 400
    assert "stance" in exc.value.message
