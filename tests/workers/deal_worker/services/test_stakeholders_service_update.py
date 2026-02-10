"""Tests for StakeholdersService.update_stakeholder."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.api.schemas.crm_models import UpdateStakeholderRequest
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
    stakeholder = SimpleNamespace(
        stakeholder_id=uuid4(),
        deal_id=uuid4(),
        email="person@example.com",
        name="Old Name",
        job_title="Old Title",
        department=None,
        external_links=None,
        stance="UNKNOWN",
        decision_power="UNKNOWN",
        champion_potential=10.0,
        motivation=None,
        pain_points=None,
        keywords=None,
        last_contact_at=None,
        engagement_score=0.0,
        temperature="UNKNOWN",
        source="MANUAL",
        approval_status="APPROVED",
        stakeholder_data=None,
    )
    service.get_stakeholder = AsyncMock(return_value=stakeholder)  # type: ignore[method-assign]

    request = UpdateStakeholderRequest(
        name="Updated Name",
        job_title="Updated Title",
        champion_potential=82.0,
    )

    result = await service.update_stakeholder(uuid4(), stakeholder.stakeholder_id, request)

    assert result.name == "Updated Name"
    assert result.job_title == "Updated Title"
    assert result.champion_potential == 82.0
    assert set(stakeholder.stakeholder_data["manual_overrides"]) >= {
        "name",
        "job_title",
        "champion_potential",
    }
    service.session.commit.assert_awaited_once()
    service.session.refresh.assert_awaited_once_with(stakeholder)


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
            UpdateStakeholderRequest.model_validate({"champion_potential": None}),
        )

    assert exc.value.status_code == 400
    assert "champion_potential" in exc.value.message
