"""Tests for UpdateDealStageFormHandler validation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from src.service.bots.form_handlers.update_deal_stage_handler import (
    UpdateDealStageFormHandler,
)


@pytest.fixture
def handler() -> UpdateDealStageFormHandler:
    return UpdateDealStageFormHandler()


@pytest.fixture
def activity() -> dict[str, Any]:
    return {
        "from": {"id": "teams-user-123"},
        "channelData": {"tenant": {"id": "tenant-456"}},
    }


VALID_DATA: dict[str, Any] = {
    "stage_id": "44444444-4444-4444-4444-444444444444",
    "deal_id": "22222222-2222-2222-2222-222222222222",
    "deal_name": "Acme Deal",
    "organisation_id": "11111111-1111-1111-1111-111111111111",
}


class TestUpdateDealStageHandler:
    """Tests for form data validation in UpdateDealStageFormHandler."""

    @pytest.mark.asyncio
    async def test_rejects_missing_stage_id(
        self, handler: UpdateDealStageFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA}
        del data["stage_id"]

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "stage" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_empty_stage_id(
        self, handler: UpdateDealStageFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA, "stage_id": ""}

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "stage" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_invalid_stage_id_uuid(
        self, handler: UpdateDealStageFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA, "stage_id": "not-a-uuid"}

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "invalid" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_invalid_deal_id_uuid(
        self, handler: UpdateDealStageFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA, "deal_id": "not-a-uuid"}

        result = await handler.handle(activity, data)

        assert result.success is False
        assert "invalid" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rejects_missing_deal_id(
        self, handler: UpdateDealStageFormHandler, activity: dict
    ) -> None:
        data = {**VALID_DATA, "deal_id": ""}

        result = await handler.handle(activity, data)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_success_includes_deal_id(
        self, handler: UpdateDealStageFormHandler, activity: dict
    ) -> None:
        organisation_id = UUID(VALID_DATA["organisation_id"])
        deal_id = UUID(VALID_DATA["deal_id"])
        user = SimpleNamespace(
            organisation_id=organisation_id,
            user_id=UUID("55555555-5555-5555-5555-555555555555"),
        )
        master_session = MagicMock()
        org_session = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
        write_service = MagicMock()
        write_service.move_stage = AsyncMock(
            return_value=SimpleNamespace(deal_id=deal_id, stage_name="Negotiation")
        )

        async def acquire_master_session():
            yield master_session

        async def acquire_org_session(_organisation_id: UUID):
            yield org_session

        with (
            patch(
                "src.service.bots.form_handlers.update_deal_stage_handler.resolve_user_from_activity",
                new=AsyncMock(return_value=(user, None)),
            ),
            patch(
                "src.infra.database.sessions.acquire_master_async_session",
                new=acquire_master_session,
            ),
            patch(
                "src.infra.database.sessions.acquire_org_async_session",
                new=acquire_org_session,
            ),
            patch(
                "src.service.crm.deals.write_service.DealWriteService",
                return_value=write_service,
            ),
        ):
            result = await handler.handle(activity, VALID_DATA)

        assert result.success is True
        assert str(deal_id) in result.message
