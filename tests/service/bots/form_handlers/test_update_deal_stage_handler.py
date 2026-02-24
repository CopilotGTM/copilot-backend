"""Tests for UpdateDealStageFormHandler validation."""

from __future__ import annotations

from typing import Any

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
