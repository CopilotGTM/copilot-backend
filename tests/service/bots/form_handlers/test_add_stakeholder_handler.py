"""Tests for AddStakeholderFormHandler validation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
