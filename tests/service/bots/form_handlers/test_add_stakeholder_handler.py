"""Tests for AddStakeholderFormHandler validation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.service.bots.form_handlers.add_stakeholder_handler import (
    AddStakeholderFormHandler,
)


@pytest.fixture
def handler() -> AddStakeholderFormHandler:
    return AddStakeholderFormHandler()


@pytest.fixture
def activity() -> dict[str, Any]:
    return {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }


VALID_DATA: dict[str, Any] = {
    "email": "bob@acme.com",
    "name": "Bob Smith",
    "deal_id": "22222222-2222-2222-2222-222222222222",
    "deal_name": "Acme Deal",
    "organisation_id": "11111111-1111-1111-1111-111111111111",
}


class TestAddStakeholderHandler:
    """Tests for form data validation in AddStakeholderFormHandler."""

    @pytest.mark.asyncio
    async def test_rejects_missing_email(
        self, handler: AddStakeholderFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA}
        del data["email"]

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "email" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_empty_email(
        self, handler: AddStakeholderFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA, "email": ""}

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "email" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_whitespace_email(
        self, handler: AddStakeholderFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA, "email": "   "}

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "email" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_missing_deal_id(
        self, handler: AddStakeholderFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA, "deal_id": ""}

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "deal" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_invalid_deal_id_uuid(
        self, handler: AddStakeholderFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA, "deal_id": "not-a-uuid"}

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "invalid" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_internal_email_via_service(
        self,
        handler: AddStakeholderFormHandler,
        activity: dict,
    ) -> None:
        """Internal team member emails rejected at service layer return clear message."""
        from uuid import UUID

        from src.service.crm.insights.stakeholders_service import StakeholdersServiceError

        # Mock user resolution
        fake_user = MagicMock()
        fake_user.organisation_id = UUID(VALID_DATA["organisation_id"])
        fake_user.user_id = UUID("33333333-3333-3333-3333-333333333333")

        # Mock service that raises 403 for internal email
        mock_service_instance = MagicMock()
        mock_service_instance.create_stakeholder = AsyncMock(
            side_effect=StakeholdersServiceError(
                "This email belongs to an internal team member",
                status_code=403,
            )
        )
        mock_service_cls = MagicMock(return_value=mock_service_instance)

        mock_session = AsyncMock()

        async def fake_acquire_gen(org_id):
            yield mock_session

        with (
            patch(
                "src.service.bots.form_handlers.add_stakeholder_handler.resolve_user_from_activity",
                new_callable=AsyncMock,
                return_value=(fake_user, None),
            ),
            patch(
                "src.infra.database.sessions.acquire_org_async_session",
                side_effect=fake_acquire_gen,
            ),
            patch(
                "src.service.crm.insights.stakeholders_service.StakeholdersService",
                mock_service_cls,
            ),
        ):
            data = {**VALID_DATA, "email": "seller@mycompany.com"}
            result = await handler.handle(activity, data)

        assert result.success is False
        assert "internal team member" in result.message.lower()

    @pytest.mark.asyncio
    async def test_success_queues_processing_jobs_and_research(
        self,
        handler: AddStakeholderFormHandler,
        activity: dict,
    ) -> None:
        """Card submissions must trigger the same participant-backed work as the API."""
        fake_user = MagicMock()
        fake_user.organisation_id = UUID(VALID_DATA["organisation_id"])
        fake_user.user_id = UUID("33333333-3333-3333-3333-333333333333")
        participant_id = uuid4()
        stakeholder = SimpleNamespace(
            participant_id=participant_id,
            email=VALID_DATA["email"],
            name=VALID_DATA["name"],
        )

        mock_service_instance = MagicMock()
        mock_service_instance.create_stakeholder = AsyncMock(return_value=stakeholder)
        mock_service_cls = MagicMock(return_value=mock_service_instance)

        mock_job_service = MagicMock()
        mock_job_service.create_stakeholder_processing_jobs = AsyncMock(return_value=[])
        mock_job_service_cls = MagicMock(return_value=mock_job_service)
        mock_session = AsyncMock()

        async def fake_acquire_gen(org_id):
            yield mock_session

        with (
            patch(
                "src.service.bots.form_handlers.add_stakeholder_handler.resolve_user_from_activity",
                new_callable=AsyncMock,
                return_value=(fake_user, None),
            ),
            patch(
                "src.infra.database.sessions.acquire_org_async_session",
                side_effect=fake_acquire_gen,
            ),
            patch(
                "src.service.crm.insights.stakeholders_service.StakeholdersService",
                mock_service_cls,
            ),
            patch(
                "src.service.crm.jobs.stakeholder_job_visibility_service."
                "StakeholderJobVisibilityService",
                mock_job_service_cls,
            ),
            patch(
                "src.workers.deal_worker.utils.stakeholder_research_dispatch."
                "dispatch_stakeholder_research_async",
                new_callable=AsyncMock,
            ) as dispatch_research,
        ):
            result = await handler.handle(activity, VALID_DATA)

        assert result.success is True
        mock_job_service.create_stakeholder_processing_jobs.assert_awaited_once_with(
            UUID(VALID_DATA["deal_id"]),
            participant_id,
        )
        dispatch_research.assert_awaited_once_with(
            organisation_id=fake_user.organisation_id,
            stakeholder_id=participant_id,
            deal_id=UUID(VALID_DATA["deal_id"]),
            user_id=fake_user.user_id,
        )
