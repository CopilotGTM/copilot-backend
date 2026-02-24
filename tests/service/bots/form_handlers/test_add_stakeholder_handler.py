"""Tests for AddStakeholderFormHandler validation."""

from __future__ import annotations

from typing import Any

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
